"""
Manual-edit / provenance-overlay semantics for the RHYTHM exam sidecar.

``ExamExtractor.extract()`` derives as much of the exam-entry form as DICOM
headers allow and leaves the rest ``None`` with an explanation in ``notes``.
The ``/studies`` dashboard page lets an operator fill in those gaps by hand.
Those hand-entered values must survive a re-extraction (e.g. after a study is
re-queued, or the operator clicks "Re-extract") without being silently
overwritten by the next automated pass — this module is the single place
that overlay logic lives, so the PUT-exam handler and the re-extract handler
apply it identically.

The overlay is recorded under a top-level ``manual`` key::

    "manual": {
      "edited_at": "2026-08-19T12:00:00+03:00",
      "fields": { "patient.weight_kg": 34.0, "image_quality": "Diagnostic" }
    }

``fields`` maps a dotted path to the value a human set. Phase fields are
keyed by ``series_number`` (``phases.series_2.dlp_mgy_cm``), never list
index — ``extract()`` rebuilds ``phases[]`` from disk on every run, sorted
by series number, so an index-keyed overlay could land on the wrong series
after a re-extraction that changes the phase set (a purged series, a
localizer newly excluded, etc).
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Dotted paths the UI is allowed to set manually. "phases.series_<N>.*" is a
# family of paths, not a literal entry — apply_manual matches it by regex.
MANUAL_EDITABLE_PATHS = {
    "patient.weight_kg",
    "patient.age_years",
    "indication.region",
    "indication.clinical_indication",
    "indication.iv_contrast",
    "image_quality",
    "protocol_used_hint.protocol_name",
    "scanner.rhythm_scanner_id",
    "phases.series_<N>.dlp_mgy_cm",
    "phases.series_<N>.ctdi_vol_mgy",
    "operator_notes",
}

# Fields addressable directly via a fixed dotted path (everything in
# MANUAL_EDITABLE_PATHS except the phases.series_<N>.* family).
_SIMPLE_PATHS = {p for p in MANUAL_EDITABLE_PATHS if "<N>" not in p}

# notes[] prefixes that _determine_bucket / extract() itself produces and
# that recompute_buckets must strip before appending its own fresh notes —
# otherwise a resolved field (e.g. weight now supplied manually) would leave
# a stale "needs manual entry" note sitting next to the now-correct value.
_STALE_NOTE_PREFIXES = (
    "PatientWeight absent",
    "PatientAge absent",
    "protocol_type=YOUNG_ADULT is a heuristic",
    "examination_group left null",
    "protocol_type/examination_group left null",
)


def _is_phase_path(path: str) -> Optional[tuple[int, str]]:
    """Return (series_number, leaf) if path is 'phases.series_<N>.<leaf>'."""
    parts = path.split(".")
    if len(parts) != 3 or parts[0] != "phases" or not parts[1].startswith("series_"):
        return None
    try:
        series_number = int(parts[1][len("series_"):])
    except ValueError:
        return None
    return series_number, parts[2]


def is_editable_path(path: str) -> bool:
    """True if *path* is a writable dotted path (including phases.series_<N>.*)."""
    return path in _SIMPLE_PATHS or _is_phase_path(path) is not None


def apply_manual(data: dict, manual: dict[str, Any]) -> dict:
    """
    Write ``manual["fields"]`` into *data*'s canonical nested structure and
    record ``data["manual"] = manual`` so the two always stay consistent.

    Unknown/unmatched phase series numbers are dropped with a note appended
    to ``data["notes"]`` rather than raising, since a phase set can shrink
    between extractions.
    """
    fields = manual.get("fields") or {}
    notes = data.setdefault("notes", [])

    for path, value in fields.items():
        phase = _is_phase_path(path)
        if phase is not None:
            series_number, leaf = phase
            target = next(
                (p for p in data.get("phases") or [] if p.get("series_number") == series_number),
                None,
            )
            if target is None:
                note = f"manual field for series {series_number} has no matching phase — discarded"
                if note not in notes:
                    notes.append(note)
                continue
            target[leaf] = value
            continue

        if path not in _SIMPLE_PATHS:
            logger.warning("Ignoring manual field with unknown path %r", path)
            continue

        _set_dotted(data, path, value)

    data["manual"] = manual
    return data


def _set_dotted(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for key in parts[:-1]:
        node = node.setdefault(key, {})
    node[parts[-1]] = value


def merge_extracted(fresh: dict, previous: dict) -> dict:
    """
    Overlay *previous*'s manual edits onto a freshly extracted exam dict,
    then recompute the derived protocol_type/examination_group buckets.
    """
    manual = previous.get("manual") or {"edited_at": None, "fields": {}}
    apply_manual(fresh, manual)
    recompute_buckets(fresh)
    return fresh


def recompute_buckets(data: dict) -> None:
    """
    Mutate *data* in place: recompute ``protocol_type`` / ``examination_group``
    from the current (possibly manually-edited) indication/age/weight, and
    refresh the bucket-related entries in ``notes`` accordingly. Everything
    else in ``notes`` (RDSR caveat, scanner-lookup miss, indication-match
    miss) passes through untouched.
    """
    # Imported lazily to avoid a module-level circular import between
    # exam_extractor (which doesn't need this module) and this one (which
    # needs exam_extractor's bucket logic) — and to keep exam_extractor's
    # only dependents importing pydicom/yaml unless they actually extract.
    from pixieveil.processing.exam_extractor import _determine_bucket

    indication = data.get("indication") or {}
    patient = data.get("patient") or {}
    region = indication.get("region")
    clinical_indication = indication.get("clinical_indication")
    age_years = patient.get("age_years")
    weight_kg = patient.get("weight_kg")

    protocol_type, examination_group, bucket_notes = _determine_bucket(
        region, clinical_indication, age_years, weight_kg
    )
    data["protocol_type"] = protocol_type
    data["examination_group"] = examination_group

    notes = data.get("notes") or []
    kept = [n for n in notes if not str(n).startswith(_STALE_NOTE_PREFIXES)]
    kept.extend(bucket_notes)
    data["notes"] = kept
