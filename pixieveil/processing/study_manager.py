"""
Study Manager Module

Manages DICOM study lifecycle: state tracking, numeric ID assignment, inactivity
timeout detection, and persistent sidecar I/O for crash recovery.

At startup, ``initialize_from_sidecars()`` scans ``base_path`` for ``????.json``
sidecar files and restores all in-memory state from them.  Studies whose sidecar
status is ``complete`` or ``defacing`` are re-queued automatically so they are
processed on the next ``check_study_completions()`` call.
"""

import hashlib
import logging
import time
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from pixieveil.config import Settings
from pixieveil.storage.study_sidecar import StudySidecar, _now_iso

logger = logging.getLogger(__name__)

# Per-study record of every image accepted while receiving, one hashed
# original SOPInstanceUID per line, so a duplicate is still recognised after
# a restart. Hashed because the study directory is what gets exported
# (ZipManager and DicomStorage skip it anyway).
RECEIVED_SOPS_FILE = ".received_sops"

# Statuses a study may be re-opened from ("allow re-receive"). Not
# 'receiving' (nothing to do) nor 'complete'/'defacing' (a _process_study
# task is working on the directory).
REOPENABLE_STATUSES = frozenset(
    {"ready", "archived", "defacing_failed", "defacing_disabled", "deleted"})

# Statuses a study may be deleted from: not while a _process_study task
# works on its directory ('complete' / 'defacing'), and not twice.
DELETABLE_STATUSES = frozenset(
    {"receiving", "ready", "archived", "defacing_failed", "defacing_disabled"})

# Highest study number ever allocated, in base_path. Study numbers must never
# be reused (exports, RHYTHM uploads and /lookup refer to them), even when a
# study's records were removed by hand; startup takes the max of this and
# the sidecars.
LAST_NUMBER_FILE = ".last_study_number"

# Throttle for persisting the dropped-re-send counter: a re-sent study can
# drop hundreds of images a second; flush_resend_counts() saves the rest.
_RESEND_SAVE_EVERY = 50


def sop_key(sop_instance_uid: str) -> str:
    """Stable, non-reversible key for an original SOPInstanceUID."""
    return hashlib.sha256(sop_instance_uid.encode()).hexdigest()[:24]


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
        self.number_map: Dict[int, str] = {}                         # study_number → orig_study_uid
        self.series_map: Dict[Tuple[str, str], Tuple[int, int]] = {} # (study_uid, series_uid) → (study_num, series_num)
        # study_num → highest series_num assigned, so a new series doesn't
        # scan every series of every study ever loaded.
        self._max_series: Dict[int, int] = {}
        self.image_counters: Dict[Tuple[int, int], int] = {}         # (study_num, series_num) → image_count

        self.study_counter: int = 0

        # Persistent state
        self._base_path: Optional[Path] = None
        self._sidecars: Dict[str, StudySidecar] = {}  # orig_study_uid → sidecar

        # orig_study_uid → sop_key()s accepted so far (lazily re-read from
        # RECEIVED_SOPS_FILE after a restart), and studies whose
        # resends_dropped counter changed but isn't saved yet.
        self._seen_sops: Dict[str, set] = {}
        self._resend_unsaved: Dict[str, int] = {}

        # Studies recovered from disk that need re-processing
        self._recovered_studies: List[str] = []

        self.lock = threading.Lock()

        # Optional hook called after every successful sidecar write (status,
        # series, RHYTHM job, …) — StorageManager points it at the dashboard
        # event bus. Must be cheap and thread-safe; exceptions are swallowed.
        self.on_change: Optional[Callable[[StudySidecar], None]] = None
        logger.debug("StudyManager initialised")

    def _save(self, sc: StudySidecar) -> None:
        """Persist *sc* and notify ``on_change``. Caller holds ``self.lock``."""
        sc.save(self._base_path)
        if self.on_change is not None:
            try:
                self.on_change(sc)
            except Exception:
                logger.warning("StudyManager.on_change hook failed", exc_info=True)

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
        self.study_counter = max(self.study_counter, self._read_last_number())

        if not base_path.exists():
            logger.debug("Base path does not exist yet — starting fresh")
            return

        sidecars = StudySidecar.load_all(base_path)
        recovered = 0

        with self.lock:
            for study_uid, sc in sidecars.items():
                # Restore UID → number mappings
                self.study_map[study_uid] = sc.study_number
                self.number_map[sc.study_number] = study_uid
                self.study_counter = max(self.study_counter, sc.study_number)

                for orig_series_uid, rec in sc.series.items():
                    self.series_map[(study_uid, orig_series_uid)] = (
                        sc.study_number, rec.series_number
                    )
                    self._max_series[sc.study_number] = max(
                        self._max_series.get(sc.study_number, 0), rec.series_number)

                # Restore image counters from the file system
                study_dir = base_path / f"{sc.study_number:04d}"
                for orig_series_uid, rec in sc.series.items():
                    series_dir = study_dir / f"{rec.series_number:04d}"
                    if series_dir.exists():
                        count = sum(1 for f in series_dir.glob("*.dcm") if f.is_file())
                        self.image_counters[(sc.study_number, rec.series_number)] = count

                # Keep the sidecar reference
                self._sidecars[study_uid] = sc

                # Re-queue studies that did not finish processing (crash before
                # reaching "ready"), and studies where a previous defacing pass
                # left a head-scan series undefaced — reusing the same recovery
                # path retries defacing for just the series that still need it
                # (is_series_defaced already makes that idempotent) rather than
                # requiring a separate "redo defacing" action. Studies already
                # "ready" or "archived" are left alone — export is manual-only,
                # never retried automatically.
                needs_requeue = (
                    sc.status in ("complete", "defacing", "defacing_failed",
                                  "defacing_disabled")
                ) and study_dir.exists()
                if needs_requeue:
                    self._recovered_studies.append(study_uid)
                    recovered += 1
                    logger.info(
                        "Study %04d recovered (status=%s, archived_via=%s) — queued for reprocessing",
                        sc.study_number, sc.status, sc.archived_via,
                    )

        logger.info(
            "Loaded %d sidecars from %s; %d studies queued for recovery; "
            "next study number %04d",
            len(sidecars), base_path, recovered, self.study_counter + 1,
        )
        # Seed the file on first start after an upgrade.
        self._write_last_number(self.study_counter)

    def _read_last_number(self) -> int:
        if self._base_path is None:
            return 0
        try:
            return int((self._base_path / LAST_NUMBER_FILE).read_text().strip() or 0)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            logger.warning("Unreadable %s — relying on the sidecars", LAST_NUMBER_FILE,
                           exc_info=True)
            return 0

    def _write_last_number(self, n: int) -> None:
        """Atomically record *n* as the highest allocated study number."""
        if self._base_path is None or n <= 0:
            return
        path = self._base_path / LAST_NUMBER_FILE
        tmp = path.with_suffix(".tmp")
        try:
            self._base_path.mkdir(parents=True, exist_ok=True)
            tmp.write_text(f"{n}\n")
            tmp.replace(path)
        except OSError:
            logger.error("Could not record last study number %d in %s", n, path,
                         exc_info=True)

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
                self.number_map[self.study_counter] = original_study_uid
                self._write_last_number(self.study_counter)
                logger.debug(
                    "New study %d assigned to %s", self.study_counter, original_study_uid
                )

            study_number = self.study_map[original_study_uid]

            # Series assignment
            series_key = (original_study_uid, original_series_uid)
            is_new_series = series_key not in self.series_map

            if is_new_series:
                series_number = self._max_series.get(study_number, 0) + 1
                self._max_series[study_number] = series_number
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
    # Re-send detection
    #
    # Anonymization gives every image fresh UIDs, so nothing downstream can
    # tell a re-sent slice from a new one: a study sent twice used to end up
    # with every slice stored twice in the same series (which then broke
    # defacing). The original SOPInstanceUID, checked here before anything
    # is stored, is the only reliable key.

    def admit_image(self, original_study_uid: str,
                    original_sop_uid: str) -> Optional[str]:
        """Decide whether an incoming image may be stored.

        Returns ``None`` to accept (and reserves the image, so a concurrent
        copy is refused — call :meth:`release_image` if storing then fails,
        :meth:`record_received_image` once it is on disk), ``"duplicate"`` for
        an image this study already holds, or ``"finished"`` for any image of
        a study past ``receiving`` (re-open it with :meth:`reopen_study` to
        receive it again).
        """
        key = sop_key(original_sop_uid)
        with self.lock:
            state = self.study_states.get(original_study_uid)
            sc = self._sidecars.get(original_study_uid)
            if (state is not None and state.completed) or (
                    sc is not None and sc.status != "receiving"):
                self._note_resend_locked(original_study_uid, sc, "finished")
                return "finished"
            seen = self._seen_sops_locked(original_study_uid, sc)
            if key in seen:
                self._note_resend_locked(original_study_uid, sc, "duplicate")
                return "duplicate"
            seen.add(key)
            return None

    def release_image(self, original_study_uid: str, original_sop_uid: str) -> None:
        """Undo :meth:`admit_image`'s reservation for an image that was not stored."""
        with self.lock:
            seen = self._seen_sops.get(original_study_uid)
            if seen is not None:
                seen.discard(sop_key(original_sop_uid))

    def record_received_image(self, original_study_uid: str, study_number: int,
                              original_sop_uid: str) -> None:
        """Persist an admitted image's key next to its study's files."""
        if self._base_path is None:
            return
        path = self._base_path / f"{study_number:04d}" / RECEIVED_SOPS_FILE
        with self.lock:
            try:
                with path.open("a") as fh:
                    fh.write(sop_key(original_sop_uid) + "\n")
            except OSError:
                logger.warning("Could not record received image in %s", path,
                               exc_info=True)

    def _seen_sops_locked(self, original_study_uid: str,
                          sc: Optional[StudySidecar]) -> set:
        seen = self._seen_sops.get(original_study_uid)
        if seen is None:
            seen = set()
            if sc is not None and self._base_path is not None:
                path = self._base_path / f"{sc.study_number:04d}" / RECEIVED_SOPS_FILE
                try:
                    seen.update(line.strip() for line in path.read_text().splitlines()
                                if line.strip())
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("Could not read %s", path, exc_info=True)
            self._seen_sops[original_study_uid] = seen
        return seen

    def _note_resend_locked(self, original_study_uid: str,
                            sc: Optional[StudySidecar], reason: str) -> None:
        if sc is None:
            return
        first = original_study_uid not in self._resend_unsaved
        if first:
            logger.warning(
                "Study %04d: dropping re-sent image(s) (%s) — %s",
                sc.study_number, reason,
                "already received" if reason == "duplicate"
                else f"study is '{sc.status}'; use 'Allow re-receive' to accept a new copy",
            )
        sc.resends_dropped += 1
        sc.last_resend_at = _now_iso()
        pending = self._resend_unsaved.get(original_study_uid, 0) + 1
        if first or pending >= _RESEND_SAVE_EVERY:
            self._save(sc)
            pending = 0
        self._resend_unsaved[original_study_uid] = pending

    def flush_resend_counts(self) -> None:
        """Save dropped-re-send counters not yet persisted, and end the
        current burst (so the next re-send logs a warning again)."""
        with self.lock:
            for uid, pending in list(self._resend_unsaved.items()):
                sc = self._sidecars.get(uid)
                if pending and sc is not None:
                    self._save(sc)
            self._resend_unsaved.clear()

    def mark_study_deleted(self, original_study_uid: str) -> Optional[str]:
        """Status ``deleted``: the caller has removed the files and exam
        record. The sidecar stays (number never reused, /lookup still
        works); defacing flags and in-memory receive state are reset, so
        'Allow re-receive' can bring the study back. Returns an error
        message, or ``None`` on success."""
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return "study not found"
            if sc.status not in DELETABLE_STATUSES:
                return f"study is '{sc.status}' — cannot delete it now"
            for rec in sc.series.values():
                rec.defaced = False
                rec.defaced_at = None
                self.image_counters[(sc.study_number, rec.series_number)] = 0
            sc.status = "deleted"
            sc.deleted_at = _now_iso()
            for attr in ("rhythm_batch", "rhythm_session_id", "rhythm_job_id",
                         "rhythm_job_status_url", "rhythm_status",
                         "rhythm_zip_sha256", "rhythm_zip_size"):
                setattr(sc, attr, None)
            self.study_states.pop(original_study_uid, None)
            self._seen_sops.pop(original_study_uid, None)
            self._resend_unsaved.pop(original_study_uid, None)
            self._recovered_studies = [
                u for u in self._recovered_studies if u != original_study_uid]
            self._save(sc)
        logger.info("Study %04d deleted (record kept)", sc.study_number)
        return None

    def reopen_study(self, original_study_uid: str) -> Optional[str]:
        """Return the study to ``receiving`` so the next send is accepted.

        The caller must already have deleted the study's files. Resets
        everything that described the old copy — defacing flags, export /
        retention stamps, the current RHYTHM job — but keeps the record's
        history (``audit``, ``rhythm_uploads``, RHYTHM ids). Returns an error
        message, or ``None`` on success.
        """
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return "study not found"
            if sc.status not in REOPENABLE_STATUSES:
                return f"study is '{sc.status}' — cannot re-open it now"
            for rec in sc.series.values():
                rec.is_head = False
                rec.is_topogram = False
                rec.defaced = False
                rec.defaced_at = None
                self.image_counters[(sc.study_number, rec.series_number)] = 0
            sc.status = "receiving"
            sc.archived_via = None
            sc.exported_at = None
            sc.purged_at = None
            sc.exported_by = None
            for attr in ("rhythm_batch", "rhythm_session_id", "rhythm_job_id",
                         "rhythm_job_status_url", "rhythm_status",
                         "rhythm_zip_sha256", "rhythm_zip_size"):
                setattr(sc, attr, None)
            sc.reopened_at = _now_iso()
            sc.deleted_at = None
            self._seen_sops.pop(original_study_uid, None)
            self._resend_unsaved.pop(original_study_uid, None)
            # A fresh StudyState arrives with the first re-sent image.
            self.study_states.pop(original_study_uid, None)
            self._recovered_studies = [
                u for u in self._recovered_studies if u != original_study_uid]
            self._save(sc)
        logger.info("Study %04d re-opened: the next send will be received as a new copy",
                    sc.study_number)
        return None

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
            self._save(sc)

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
            self._save(sc)

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
            self._save(sc)

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

    def get_sidecar_by_number(self, study_number: int) -> Optional[StudySidecar]:
        """O(1) reverse lookup via number_map, instead of scanning every sidecar."""
        with self.lock:
            uid = self.number_map.get(study_number)
            return self._sidecars.get(uid) if uid is not None else None

    def all_sidecars(self) -> list[StudySidecar]:
        """Every known study's live sidecar (a list copy, so a study added
        meanwhile can't break iteration). Authoritative: every sidecar write
        goes through :meth:`_save`, and startup loads all of them, archived
        and purged included — so callers needn't re-read ``*.state.json``."""
        with self.lock:
            return list(self._sidecars.values())

    def get_sidecar_by_uid(self, original_study_uid: str) -> Optional[StudySidecar]:
        with self.lock:
            return self._sidecars.get(original_study_uid)

    def has_undefaced_head_series(self, original_study_uid: str) -> bool:
        """True if any series flagged is_head=True still has defaced=False."""
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            return sc.has_undefaced_head_series() if sc else False

    # ------------------------------------------------------------------
    # Study lifecycle

    def check_study_completions(self) -> List[str]:
        """
        Return UIDs of studies ready to be processed.

        Drains the crash-recovery queue first, then checks active studies for
        inactivity timeout.  Marks returned studies as ``complete`` in memory
        and updates their sidecar status accordingly.
        """
        self.flush_resend_counts()
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

    def mark_study_ready(self, original_study_uid: str) -> None:
        """Called once exam extraction/defacing finish, with no export attempted.

        The study now sits in ``base_path`` awaiting a manual export/archive
        action from the ``/studies`` dashboard — see ``StorageManager.manual_send_dicom``
        and ``manual_upload_http``.
        """
        self._update_sidecar_status(original_study_uid, "ready")
        with self.lock:
            self.study_states.pop(original_study_uid, None)

    def mark_study_defacing_failed(self, original_study_uid: str) -> None:
        """Called when a defacing pass finishes but at least one head-scan
        series never got defaced — never "ready", so manual export stays
        blocked (see StorageManager.manual_send_dicom/manual_upload_http)
        instead of silently letting an undefaced study out. Included in the
        crash-recovery requeue set so a restart retries the series that
        still need it.
        """
        self._update_sidecar_status(original_study_uid, "defacing_failed")
        with self.lock:
            self.study_states.pop(original_study_uid, None)

    def mark_study_defacing_disabled(self, original_study_uid: str) -> None:
        """Called when a study has a head-scan series but defacing is turned
        off in config. Export stays blocked like defacing_failed, and the
        study is requeued on restart so enabling defacing then defaces it.
        """
        self._update_sidecar_status(original_study_uid, "defacing_disabled")
        with self.lock:
            self.study_states.pop(original_study_uid, None)

    def mark_study_archived(self, original_study_uid: str,
                             via: Optional[str] = None) -> None:
        """Mark a study as fully processed and remove it from active tracking.

        ``via`` should be ``"dicom"``, ``"http"`` or ``"rhythm"`` when the
        study was exported remotely, or ``None`` when it is kept locally.
        """
        if self._base_path is not None:
            with self.lock:
                sc = self._sidecars.get(original_study_uid)
                if sc is not None:
                    sc.archived_via = via
        self._update_sidecar_status(original_study_uid, "archived")
        with self.lock:
            self.study_states.pop(original_study_uid, None)

    def mark_study_exported(self, original_study_uid: str, when: str) -> None:
        """Stamp ``exported_at`` on first successful export (the retention
        clock). No-op if the sidecar is unknown / persistence disabled."""
        self._stamp_sidecar(original_study_uid, exported_at=when)

    def mark_study_purged(self, original_study_uid: str, when: str) -> None:
        """Stamp ``purged_at`` once the retention window removed a study's
        bulky artifacts (directory / export ZIP / deface scratch), leaving
        only its records."""
        self._stamp_sidecar(original_study_uid, purged_at=when)

    def _stamp_sidecar(self, original_study_uid: str, **fields: object) -> None:
        if self._base_path is None:
            return
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return
            for key, value in fields.items():
                setattr(sc, key, value)
            self._save(sc)

    def _update_sidecar_status(self, original_study_uid: str, status: str) -> None:
        if self._base_path is None:
            return
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return
            sc.status = status
            self._save(sc)

    # Entries kept per study; the oldest are dropped beyond this so a
    # study that is re-sent many times can't grow its sidecar unbounded.
    AUDIT_MAX_ENTRIES = 200

    def append_audit(self, original_study_uid: str, entry: dict,
                     exported_by: Optional[str] = None) -> None:
        """Append one attribution entry to the sidecar's ``audit`` list and,
        if given and not yet set, stamp ``exported_by``. No-op if the
        sidecar is unknown or persistence is disabled."""
        if self._base_path is None:
            return
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return
            sc.audit.append(entry)
            if len(sc.audit) > self.AUDIT_MAX_ENTRIES:
                del sc.audit[:-self.AUDIT_MAX_ENTRIES]
            if exported_by and not sc.exported_by:
                sc.exported_by = exported_by
            self._save(sc)

    _RHYTHM_JOB_FIELDS = frozenset(
        {"batch", "session_id", "job_id", "job_status_url", "status",
         "zip_sha256", "zip_size", "repository_study_id", "orthanc_study_id"}
    )
    # Fields mirrored into each rhythm_uploads[] history entry.
    _RHYTHM_HISTORY_FIELDS = ("batch", "session_id", "job_id", "status",
                              "zip_sha256", "zip_size", "repository_study_id",
                              "orthanc_study_id")

    def set_rhythm_job(self, original_study_uid: str, **fields: object) -> None:
        """Persist RHYTHM upload job-tracking onto the study sidecar.

        Accepts ``batch``/``session_id``/``job_id``/``job_status_url``/
        ``status``/``zip_sha256``/``zip_size`` (written to the matching
        ``rhythm_*`` attribute); other keys are ignored. A ``None`` value
        clears its field. ``zip_size`` is an int, the rest are strings.
        No-op if the sidecar is unknown or sidecar persistence is disabled.
        """
        if self._base_path is None:
            return
        with self.lock:
            sc = self._sidecars.get(original_study_uid)
            if sc is None:
                return
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            history = sc.rhythm_uploads
            if not history and sc.rhythm_batch and "batch" not in fields:
                # Sidecar from before the history existed: seed it from the
                # latest-attempt fields so the update lands somewhere.
                history.append({"started_at": None, **{
                    k: getattr(sc, f"rhythm_{k}") for k in self._RHYTHM_HISTORY_FIELDS}})
            for key, value in fields.items():
                if key in self._RHYTHM_JOB_FIELDS:
                    setattr(sc, f"rhythm_{key}", value)
            if "batch" in fields:
                # A new upload attempt (manual_upload_rhythm sets batch).
                history.append({"started_at": now})
                # RHYTHM ids belong to the attempt that produced them.
                for key in ("repository_study_id", "orthanc_study_id"):
                    if key not in fields:
                        setattr(sc, f"rhythm_{key}", None)
            if history:
                entry = history[-1]
                for key in self._RHYTHM_HISTORY_FIELDS:
                    entry[key] = getattr(sc, f"rhythm_{key}")
                entry["updated_at"] = now
            self._save(sc)

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
