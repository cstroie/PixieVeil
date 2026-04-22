"""
Study Manager Module

Manages DICOM study lifecycle: state tracking, numeric ID assignment, inactivity
timeout detection, and persistent sidecar I/O for crash recovery.

At startup, ``initialize_from_sidecars()`` scans ``base_path`` for ``????.json``
sidecar files and restores all in-memory state from them.  Studies whose sidecar
status is ``complete`` or ``defacing`` are re-queued automatically so they are
processed on the next ``check_study_completions()`` call.
"""

import logging
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pixieveil.config import Settings
from pixieveil.storage.study_sidecar import StudySidecar

logger = logging.getLogger(__name__)


class StudyState:
    """In-memory runtime state for one active study."""

    def __init__(self):
        self.last_received: float = time.time()
        self.completed: bool = False


class StudyManager:
    """
    Manages DICOM studies and their complete lifecycle.

    Numeric ID assignment, completion detection, and sidecar-backed persistence
    are all handled here.  All public methods are thread-safe.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.completion_timeout: int = settings.study.get("completion_timeout", 120)

        # In-memory runtime state (rebuilt from sidecars at startup)
        self.study_states: Dict[str, StudyState] = {}
        self.completed_count: int = 0

        # UID → number mappings (restored from sidecars at startup)
        self.study_map: Dict[str, int] = {}                          # orig_study_uid → study_number
        self.series_map: Dict[Tuple[str, str], Tuple[int, int]] = {} # (study_uid, series_uid) → (study_num, series_num)
        self.image_counters: Dict[Tuple[int, int], int] = {}         # (study_num, series_num) → image_count

        self.study_counter: int = 0

        # Persistent state
        self._base_path: Optional[Path] = None
        self._sidecars: Dict[str, StudySidecar] = {}  # orig_study_uid → sidecar

        # Studies recovered from disk that need re-processing
        self._recovered_studies: List[str] = []

        self.lock = threading.Lock()
        logger.debug("StudyManager initialised")

    # ------------------------------------------------------------------
    # Startup

    def initialize_from_sidecars(self, base_path: Path) -> None:
        """
        Restore all in-memory state from ``????.json`` sidecars in *base_path*.

        Studies with status ``complete`` or ``defacing`` that still have a
        study directory on disk are added to an internal recovery queue and
        will be returned by the next ``check_study_completions()`` call.

        Image counters are re-derived by counting ``.dcm`` files in each
        series directory (avoids storing per-image data in the sidecar).

        Call once at startup, before the first image arrives.
        """
        self._base_path = base_path

        if not base_path.exists():
            logger.debug("Base path does not exist yet — starting fresh")
            return

        sidecars = StudySidecar.load_all(base_path)
        recovered = 0

        with self.lock:
            for study_uid, sc in sidecars.items():
                # Restore UID → number mappings
                self.study_map[study_uid] = sc.study_number
                self.study_counter = max(self.study_counter, sc.study_number)

                for orig_series_uid, rec in sc.series.items():
                    self.series_map[(study_uid, orig_series_uid)] = (
                        sc.study_number, rec.series_number
                    )

                # Restore image counters from the file system
                study_dir = base_path / f"{sc.study_number:04d}"
                for orig_series_uid, rec in sc.series.items():
                    series_dir = study_dir / f"{rec.series_number:04d}"
                    if series_dir.exists():
                        count = sum(1 for f in series_dir.glob("*.dcm") if f.is_file())
                        self.image_counters[(sc.study_number, rec.series_number)] = count

                # Keep the sidecar reference
                self._sidecars[study_uid] = sc

                # Re-queue studies that did not finish processing, and studies
                # that were kept locally (archived_via=None) with their directory
                # still on disk so they can be exported now that remote may be
                # configured.
                needs_requeue = (
                    sc.status in ("complete", "defacing")
                    or (sc.status == "archived" and sc.archived_via is None)
                ) and study_dir.exists()
                if needs_requeue:
                    self._recovered_studies.append(study_uid)
                    recovered += 1
                    logger.info(
                        "Study %04d recovered (status=%s, archived_via=%s) — queued for reprocessing",
                        sc.study_number, sc.status, sc.archived_via,
                    )

        logger.info(
            "Loaded %d sidecars from %s; %d studies queued for recovery",
            len(sidecars), base_path, recovered,
        )

    # ------------------------------------------------------------------
    # Image ingestion

    def add_image_to_study(self, original_study_uid: str,
                           original_series_uid: str) -> Tuple[int, int, int, bool]:
        """
        Assign numeric IDs for an incoming image and update study state.

        Returns ``(study_number, series_number, image_number, is_new_series)``.
        """
        with self.lock:
            # Study assignment
            if original_study_uid not in self.study_map:
                self.study_counter += 1
                self.study_map[original_study_uid] = self.study_counter
                logger.debug(
                    "New study %d assigned to %s", self.study_counter, original_study_uid
                )

            study_number = self.study_map[original_study_uid]

            # Series assignment
            series_key = (original_study_uid, original_series_uid)
            is_new_series = series_key not in self.series_map

            if is_new_series:
                existing = [
                    sn for (suid, _), (stnum, sn) in self.series_map.items()
                    if stnum == study_number
                ]
                series_number = max(existing) + 1 if existing else 1
                self.series_map[series_key] = (study_number, series_number)
                logger.debug(
                    "New series %d assigned to %s in study %d",
                    series_number, original_series_uid, study_number,
                )
            else:
                study_number, series_number = self.series_map[series_key]

            # Image numbering
            image_key = (study_number, series_number)
            self.image_counters[image_key] = self.image_counters.get(image_key, 0) + 1
            image_number = self.image_counters[image_key]

            # Study state
            if original_study_uid not in self.study_states:
                self.study_states[original_study_uid] = StudyState()
            else:
                self.study_states[original_study_uid].last_received = time.time()

            return study_number, series_number, image_number, is_new_series

    # ------------------------------------------------------------------
    # Sidecar writers (called from StorageManager after anonymization)

    def record_new_series(self, original_study_uid: str, original_series_uid: str,
                          original_patient_id: str,
                          anonymized_study_uid: str, anonymized_series_uid: str,
                          anonymized_patient_id: str,
                          study_number: int, series_number: int) -> None:
        """
        Create or update the sidecar when a new study or series is first seen.

        Must be called after ``add_image_to_study()`` returns ``is_new_series=True``,
        with the anonymized UIDs already resolved.
        """
        if self._base_path is None:
            return

        with self.lock:
            if original_study_uid not in self._sidecars:
                sc = StudySidecar.create(
                    study_number=study_number,
                    original_study_uid=original_study_uid,
                    original_patient_id=original_patient_id,
                    anonymized_study_uid=anonymized_study_uid,
                    anonymized_patient_id=anonymized_patient_id,
                )
                self._sidecars[original_study_uid] = sc
            else:
                sc = self._sidecars[original_study_uid]

            sc.add_series(original_series_uid, series_number, anonymized_series_uid)
            sc.save(self._base_path)

    def set_series_classification(self, original_study_uid: str,
                                   original_series_uid: str,
                                   is_head: bool, is_topogram: bool) -> None:
        """Persist head/topogram classification for a series."""
        if self._base_path is None:
            return
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return
            sc.set_series_classification(original_series_uid, is_head, is_topogram)
            sc.save(self._base_path)

    def mark_series_defaced(self, original_study_uid: str,
                             original_series_uid: str) -> None:
        """Record that a series has been successfully defaced."""
        if self._base_path is None:
            return
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return
            sc.mark_series_defaced(original_series_uid)
            sc.save(self._base_path)

    def is_series_defaced(self, original_study_uid: str,
                           original_series_uid: str) -> bool:
        """Return True if the series was already defaced (sidecar says so)."""
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            return sc.is_series_defaced(original_series_uid) if sc else False

    def get_original_series_uid(self, original_study_uid: str,
                                 series_number: int) -> Optional[str]:
        """Reverse lookup: series_number → original_series_uid."""
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            return sc.get_series_uid_for_number(series_number) if sc else None

    # ------------------------------------------------------------------
    # Study lifecycle

    def check_study_completions(self) -> List[str]:
        """
        Return UIDs of studies ready to be processed.

        Drains the crash-recovery queue first, then checks active studies for
        inactivity timeout.  Marks returned studies as ``complete`` in memory
        and updates their sidecar status accordingly.
        """
        now = time.time()
        completed: List[str] = []

        with self.lock:
            # Recovered studies take priority
            if self._recovered_studies:
                completed.extend(self._recovered_studies)
                self._recovered_studies.clear()

            # Normal timeout-based completion
            for study_uid, state in list(self.study_states.items()):
                if study_uid in completed:
                    continue
                if not state.completed and (now - state.last_received) > self.completion_timeout:
                    logger.info(
                        "Study %s timed out after %.1fs",
                        study_uid, now - state.last_received,
                    )
                    completed.append(study_uid)
                    state.completed = True
                    self.completed_count += 1

        # Update sidecar status outside the lock (save() is atomic)
        for study_uid in completed:
            self._update_sidecar_status(study_uid, "complete")

        return completed

    def mark_study_defacing(self, original_study_uid: str) -> None:
        """Called just before defacing begins for a study."""
        self._update_sidecar_status(original_study_uid, "defacing")

    def mark_study_archived(self, original_study_uid: str,
                             via: Optional[str] = None) -> None:
        """Mark a study as fully processed and remove it from active tracking.

        ``via`` should be ``"dicom"`` or ``"http"`` when the study was
        exported remotely, or ``None`` when it is kept locally.
        """
        if self._base_path is not None:
            with self.lock:
                sc = self._sidecars.get(original_study_uid)
                if sc is not None:
                    sc.archived_via = via
        self._update_sidecar_status(original_study_uid, "archived")
        with self.lock:
            self.study_states.pop(original_study_uid, None)

    def _update_sidecar_status(self, original_study_uid: str, status: str) -> None:
        if self._base_path is None:
            return
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return
            sc.status = status
            sc.save(self._base_path)

    # ------------------------------------------------------------------
    # Queries

    def get_study_number(self, original_study_uid: str) -> Optional[int]:
        with self.lock:
            return self.study_map.get(original_study_uid)

    def get_next_image_number(self, study_number: int, series_number: int) -> int:
        with self.lock:
            return self.image_counters.get((study_number, series_number), 0)

    def get_active_study_count(self) -> int:
        with self.lock:
            return sum(1 for s in self.study_states.values() if not s.completed)

    def get_completed_study_count(self) -> int:
        with self.lock:
            return self.completed_count

    def get_active_study_numbers(self) -> set:
        with self.lock:
            return {
                num for uid, num in self.study_map.items()
                if uid in self.study_states and not self.study_states[uid].completed
            }
