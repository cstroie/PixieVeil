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

``fields`` maps a dotted path to the value a human set. Per-series fields are
keyed by ``series_number`` (``series.2.dlp_mgy_cm``), never list index —
``extract()`` rebuilds ``series[]`` from disk on every run, sorted by series
number, so an index-keyed overlay could land on the wrong series after a
re-extraction that changes the set (a purged series, a new one, etc).

Note ``series[]`` here means DICOM series (reconstructions) — NOT the same
thing as an "irradiation event". One RDSR acquisition (one real dose event)
can be reconstructed into several series at different kernel/thickness, so
several ``series[]`` entries can share one event; the true per-event dose
record is ``rdsr.acquisitions[]``, which this module never touches — it has
no stable per-item key to overlay onto and isn't user-editable.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Dotted paths the UI is allowed to set manually. "series.<N>.*" is a family
# of paths, not a literal entry — apply_manual matches it by regex.
MANUAL_EDITABLE_PATHS = {
    "patient.weight_kg",
    "patient.age_years",
    "indication.region",
    "indication.clinical_indication",
    "indication.iv_contrast",
    "image_quality",
    "protocol_used_hint.protocol_name",
    "scanner.rhythm_scanner_id",
    "series.<N>.dlp_mgy_cm",
    "series.<N>.ctdi_vol_mgy",
    "series.<N>.slice_thickness_mm",
    "series.<N>.exposure_time_ms",
    "series.<N>.convolution_kernel",
    "series.<N>.patient_position",
    "series.<N>.single_collimation_width_mm",
    "series.<N>.total_collimation_width_mm",
    "series.<N>.spiral_pitch_factor",
    "operator_notes",
}

# Fields addressable directly via a fixed dotted path (everything in
# MANUAL_EDITABLE_PATHS except the series.<N>.* family).
_SIMPLE_PATHS = {p for p in MANUAL_EDITABLE_PATHS if "<N>" not in p}

# Leaf names allowed after "series.<N>." — derived from the series.<N>.*
# entries in MANUAL_EDITABLE_PATHS, so a path like "series.3.patient_name"
# (right shape, arbitrary leaf) is rejected instead of silently writing an
# unexpected key into the series dict.
_SERIES_LEAVES = {p.rsplit(".", 1)[1] for p in MANUAL_EDITABLE_PATHS if "<N>" in p}

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


def _is_series_path(path: str) -> Optional[tuple[int, str]]:
    """Return (series_number, leaf) if path is 'series.<N>.<leaf>' and leaf
    is one of the whitelisted per-series fields — not just shape-matching."""
    parts = path.split(".")
    if len(parts) != 3 or parts[0] != "series" or parts[2] not in _SERIES_LEAVES:
        return None
    try:
        series_number = int(parts[1])
    except ValueError:
        return None
    return series_number, parts[2]


def is_editable_path(path: str) -> bool:
    """True if *path* is a writable dotted path (including series.<N>.*)."""
    return path in _SIMPLE_PATHS or _is_series_path(path) is not None


def apply_manual(data: dict, manual: dict[str, Any]) -> dict:
    """
    Write ``manual["fields"]`` into *data*'s canonical nested structure and
    record ``data["manual"] = manual`` so the two always stay consistent.

    Unknown/unmatched series numbers are dropped with a note appended to
    ``data["notes"]`` rather than raising, since the series set can change
    between extractions.
    """
    fields = manual.get("fields") or {}
    notes = data.setdefault("notes", [])

    for path, value in fields.items():
        series_path = _is_series_path(path)
        if series_path is not None:
            series_number, leaf = series_path
            target = next(
                (s for s in data.get("series") or [] if s.get("series_number") == series_number),
                None,
            )
            if target is None:
                note = f"manual field for series {series_number} has no matching series — discarded"
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
