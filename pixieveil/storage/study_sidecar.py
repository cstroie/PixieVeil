"""
Persistent JSON sidecar for per-study state.

One file per study, stored as ``<base_path>/<NNNN>.json`` (sibling of the
study directory).  Writes are atomic: the payload is written to a ``.tmp``
sibling and then renamed over the real file — rename(2) is atomic on Linux so
a crash mid-write leaves either the old version or the new version intact,
never a partial file.

Layout::

    data/dicom/
      0001/           ← study directory
        0001/         ← series directories
        0002/
      0001.json       ← sidecar (this module)
      0002/
      0002.json
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SeriesRecord:
    series_number: int
    anonymized_series_uid: str
    is_head: bool = False
    is_topogram: bool = False
    defaced: bool = False
    defaced_at: Optional[str] = None


@dataclass
class StudySidecar:
    """
    Persistent state for one study.

    ``status`` lifecycle::

        receiving → complete → defacing → archived

    - ``receiving``  images still arriving
    - ``complete``   inactivity timeout fired; ready to deface + archive
    - ``defacing``   defacing in progress (crash here → re-run on restart,
                     skipping series already marked ``defaced: true``)
    - ``archived``   ZIP created (and uploaded if configured); terminal state
    """

    study_number: int
    status: str                  # receiving | complete | defacing | archived
    original_study_uid: str
    original_patient_id: str
    anonymized_study_uid: str
    anonymized_patient_id: str
    received_at: str
    last_received_at: str
    series: Dict[str, SeriesRecord] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction helpers

    @classmethod
    def create(cls, study_number: int,
               original_study_uid: str, original_patient_id: str,
               anonymized_study_uid: str, anonymized_patient_id: str) -> "StudySidecar":
        now = _now_iso()
        return cls(
            study_number=study_number,
            status="receiving",
            original_study_uid=original_study_uid,
            original_patient_id=original_patient_id,
            anonymized_study_uid=anonymized_study_uid,
            anonymized_patient_id=anonymized_patient_id,
            received_at=now,
            last_received_at=now,
        )

    # ------------------------------------------------------------------
    # Mutation helpers (all mutate in place; caller must call save())

    def touch(self) -> None:
        self.last_received_at = _now_iso()

    def add_series(self, original_series_uid: str, series_number: int,
                   anonymized_series_uid: str) -> None:
        if original_series_uid not in self.series:
            self.series[original_series_uid] = SeriesRecord(
                series_number=series_number,
                anonymized_series_uid=anonymized_series_uid,
            )
        self.touch()

    def set_series_classification(self, original_series_uid: str,
                                   is_head: bool, is_topogram: bool) -> None:
        rec = self.series.get(original_series_uid)
        if rec is not None:
            rec.is_head = is_head
            rec.is_topogram = is_topogram

    def mark_series_defaced(self, original_series_uid: str) -> None:
        rec = self.series.get(original_series_uid)
        if rec is not None:
            rec.defaced = True
            rec.defaced_at = _now_iso()

    def is_series_defaced(self, original_series_uid: str) -> bool:
        rec = self.series.get(original_series_uid)
        return rec.defaced if rec is not None else False

    def get_series_uid_for_number(self, series_number: int) -> Optional[str]:
        """Reverse lookup: series_number → original_series_uid."""
        for uid, rec in self.series.items():
            if rec.series_number == series_number:
                return uid
        return None

    # ------------------------------------------------------------------
    # Serialisation

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "StudySidecar":
        series_raw = data.pop("series", {})
        obj = cls(**data)
        obj.series = {uid: SeriesRecord(**rec) for uid, rec in series_raw.items()}
        return obj

    # ------------------------------------------------------------------
    # I/O

    @staticmethod
    def path_for(base_path: Path, study_number: int) -> Path:
        return base_path / f"{study_number:04d}.json"

    def save(self, base_path: Path) -> None:
        """Atomically write this sidecar to disk."""
        path = self.path_for(base_path, self.study_number)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2))
            tmp.rename(path)
            logger.debug("Saved sidecar %s (status=%s)", path.name, self.status)
        except Exception:
            logger.exception("Failed to write sidecar %s", path)
            tmp.unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> "StudySidecar":
        data = json.loads(path.read_text())
        return cls.from_dict(data)

    @classmethod
    def load_all(cls, base_path: Path) -> Dict[str, "StudySidecar"]:
        """
        Load every ``????.json`` sidecar under *base_path*.

        Returns a dict keyed by ``original_study_uid``.  Corrupt files are
        skipped with a warning so a single bad sidecar does not block startup.
        """
        sidecars: Dict[str, "StudySidecar"] = {}
        for p in sorted(base_path.glob("????.json")):
            try:
                sc = cls.load(p)
                sidecars[sc.original_study_uid] = sc
                logger.debug("Loaded sidecar %s (status=%s)", p.name, sc.status)
            except Exception:
                logger.warning("Corrupt or unreadable sidecar %s — skipping", p, exc_info=True)
        return sidecars
