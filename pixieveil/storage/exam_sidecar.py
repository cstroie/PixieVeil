"""
Persistent JSON sidecar for RHYTHM exam-data extraction.

One file per study, stored as ``<base_path>/<NNNN>_exam.json`` (sibling of
the per-study StudySidecar at ``<NNNN>.json`` — the ``_exam`` suffix keeps
it out of StudySidecar.load_all()'s ``????.json`` glob). Written once, when
the study completes, from whatever the DICOM headers reveal; see
pixieveil.processing.exam_extractor for what is and isn't derivable.

Writes are atomic: the payload is written to a ``.tmp`` sibling and then
renamed over the real file, same as StudySidecar.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class ExamSidecar:
    """Thin wrapper around the exam-data dict produced by ExamExtractor."""

    def __init__(self, study_number: int, data: Dict[str, Any]):
        self.study_number = study_number
        self.data = data

    @staticmethod
    def path_for(base_path: Path, study_number: int) -> Path:
        return base_path / f"{study_number:04d}_exam.json"

    def save(self, base_path: Path) -> None:
        """Atomically write this sidecar to disk."""
        path = self.path_for(base_path, self.study_number)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.rename(path)
            logger.debug("Saved exam sidecar %s", path.name)
        except Exception:
            logger.exception("Failed to write exam sidecar %s", path)
            tmp.unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> "ExamSidecar":
        data = json.loads(path.read_text())
        return cls(study_number=data["study_number"], data=data)
