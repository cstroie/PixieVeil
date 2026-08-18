"""
RHYTHM exam-data extraction.

Reads the (already anonymized) DICOM files of a completed study and derives
as many fields as possible for the RHYTHM "Manual Exam Entry" form
(see integrations/rhythm/). Fields DICOM cannot answer — image quality,
which saved protocol was followed, DLP without an RDSR object, etc. — are
left ``None`` with an explanatory entry in ``notes`` rather than guessed.

Two small curated lookup tables live in ``integrations/rhythm/`` and are
loaded if present:
  - indication_lookup.yaml: free-text protocol/description -> RHYTHM
    (region, clinical_indication, iv_contrast) triple.
  - scanner_lookup.yaml: (manufacturer, model) -> RHYTHM scanner UUID.

Both are optional; a missing file just means those fields stay unresolved.

Classes:
    ExamExtractor: Reads a study directory and builds the exam-data dict
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pydicom
import yaml

logger = logging.getLogger(__name__)

# Indications that use the Pediatric Head tab (age-bucketed); everything else
# uses Pediatric Body (weight-bucketed). Mirrors HEAD_INDICATIONS in RHYTHM's
# protocol_clinical_gui.js.
HEAD_INDICATIONS = {
    ("Head", "Trauma"),
    (
        "Mastoid bone/Inner Ear",
        "Hearing loss; congenital malformations, infection, cholesteatoma, cochlear implants",
    ),
}

# (upper_bound_years_exclusive, examination_group) — checked in order
HEAD_AGE_GROUPS: List[Tuple[float, str]] = [
    (0.25, "Group 1 – Neonate"),
    (1.0, "Group 2 – Infant"),
    (6.0, "Group 3 – Early Childhood"),
    (float("inf"), "Group 4 – Childhood"),
]

# (upper_bound_kg_exclusive, examination_group) — checked in order
BODY_WEIGHT_GROUPS: List[Tuple[float, str]] = [
    (5.0, "Group 1 – Neonate"),
    (15.0, "Group 2 – Infant, Toddler and Early Childhood"),
    (30.0, "Group 3 – Childhood"),
    (50.0, "Group 4 – Early Adolescence"),
    (float("inf"), "Group 5 – Adolescence"),
]

YOUNG_ADULT_GROUP = "Group 6 – Adolescence & Young Adulthood"


def _parse_dicom_age(value: Optional[str]) -> Optional[float]:
    """Parse a DICOM AS value (e.g. '011Y', '006M', '023D') to years."""
    if not value:
        return None
    value = str(value).strip()
    if len(value) < 4 or not value[:3].isdigit():
        return None
    n = int(value[:3])
    unit = value[3].upper()
    if unit == "Y":
        return float(n)
    if unit == "M":
        return n / 12.0
    if unit == "W":
        return n / 52.1775
    if unit == "D":
        return n / 365.25
    return None


class ExamExtractor:
    """Derives RHYTHM exam-entry fields from a completed study's DICOM files."""

    def __init__(self, lookup_dir: Optional[Path] = None):
        self.lookup_dir = lookup_dir or Path("integrations/rhythm")
        self.indication_rules = self._load_yaml_list("indication_lookup.yaml")
        self.scanner_map = self._load_yaml_dict("scanner_lookup.yaml")

    def _load_yaml_list(self, filename: str) -> List[Dict[str, Any]]:
        path = self.lookup_dir / filename
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text()) or []
            return data.get("rules", []) if isinstance(data, dict) else list(data)
        except Exception:
            logger.warning("Could not read %s — indication matching disabled", path, exc_info=True)
            return []

    def _load_yaml_dict(self, filename: str) -> Dict[str, str]:
        path = self.lookup_dir / filename
        if not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.warning("Could not read %s — scanner matching disabled", path, exc_info=True)
            return {}

    # ------------------------------------------------------------------

    def extract(self, study_dir: Path, study_number: int,
                anonymized_study_uid: str) -> Dict[str, Any]:
        """Read every .dcm in study_dir and build the exam-data dict."""
        notes: List[str] = []
        series: Dict[str, Dict[str, Any]] = {}
        manufacturer = model = None
        protocol_name = study_description = body_part = ""
        contrast_seen = False
        age_years: Optional[float] = None
        weight_kg: Optional[float] = None

        dcm_files = [
            f for f in study_dir.rglob("*.dcm")
            if not any(part.endswith("_pre_deface") for part in f.parts)
        ]
        for f in dcm_files:
            try:
                ds = pydicom.dcmread(f, stop_before_pixels=True, force=True)
            except Exception:
                continue

            manufacturer = manufacturer or str(getattr(ds, "Manufacturer", "") or "") or None
            model = model or str(getattr(ds, "ManufacturerModelName", "") or "") or None
            protocol_name = protocol_name or str(getattr(ds, "ProtocolName", "") or "")
            study_description = study_description or str(getattr(ds, "StudyDescription", "") or "")
            body_part = body_part or str(getattr(ds, "BodyPartExamined", "") or "")

            if str(getattr(ds, "ContrastBolusAgent", "") or "").strip():
                contrast_seen = True

            if age_years is None:
                age_years = _parse_dicom_age(getattr(ds, "PatientAge", None))

            if weight_kg is None:
                raw_weight = getattr(ds, "PatientWeight", None)
                if raw_weight not in (None, ""):
                    try:
                        weight_kg = float(raw_weight)
                    except (TypeError, ValueError):
                        pass

            series_number = getattr(ds, "SeriesNumber", None)
            key = str(series_number) if series_number is not None else "?"
            rec = series.setdefault(key, {
                "series_number": series_number,
                "series_description": str(getattr(ds, "SeriesDescription", "") or ""),
                "ctdi_vol_mgy": None,
                "dlp_mgy_cm": None,
            })
            ctdi = getattr(ds, "CTDIvol", None)
            if ctdi is not None:
                try:
                    ctdi = float(ctdi)
                    if rec["ctdi_vol_mgy"] is None or ctdi > rec["ctdi_vol_mgy"]:
                        rec["ctdi_vol_mgy"] = ctdi
                except (TypeError, ValueError):
                    pass

        if not dcm_files:
            notes.append("No .dcm files found under study_dir at extraction time")

        if weight_kg is None:
            notes.append("PatientWeight absent on all images — needs manual entry")
        if age_years is None:
            notes.append("PatientAge absent or unparseable — needs manual entry")

        notes.append(
            "dlp_mgy_cm always null: no RDSR/Dose SR object present in this study; "
            "either configure the scanner to send its dose report to PixieVeil, "
            "or compute DLP = CTDIvol x scan length manually"
        )

        region, indication, iv_contrast, indication_matched = self._match_indication(
            protocol_name, study_description, body_part
        )
        if not indication_matched:
            notes.append(
                f"No indication_lookup.yaml match for "
                f"protocol_name={protocol_name!r} study_description={study_description!r} "
                f"body_part_examined={body_part!r} — needs manual selection or a new lookup rule"
            )

        contrast = None
        if indication_matched:
            contrast = iv_contrast
        else:
            contrast = "Contrast-enhanced" if contrast_seen else "Non-contrast"

        scanner_id = None
        if manufacturer and model:
            scanner_id = self.scanner_map.get(f"{manufacturer}|{model}")
            if scanner_id is None:
                notes.append(
                    f"No scanner_lookup.yaml entry for manufacturer={manufacturer!r} "
                    f"model={model!r} — add one or select the scanner manually"
                )

        protocol_type, examination_group, bucket_notes = self._determine_bucket(
            region, indication, age_years, weight_kg
        )
        notes.extend(bucket_notes)

        return {
            "study_number": study_number,
            "source": {
                "anonymized_study_uid": anonymized_study_uid,
            },
            "indication": {
                "region": region,
                "clinical_indication": indication,
                "iv_contrast": contrast,
                "matched": indication_matched,
            },
            "protocol_type": protocol_type,
            "examination_group": examination_group,
            "scanner": {
                "manufacturer": manufacturer,
                "model": model,
                "rhythm_scanner_id": scanner_id,
            },
            "protocol_used_hint": {
                "protocol_name": protocol_name or None,
                "series_description_sample": next(
                    (r["series_description"] for r in series.values() if r["series_description"]),
                    None,
                ),
            },
            "patient": {
                "age_years": age_years,
                "weight_kg": weight_kg,
            },
            "phases": [
                {
                    "series_number": rec["series_number"],
                    "series_description": rec["series_description"] or None,
                    "ctdi_vol_mgy": rec["ctdi_vol_mgy"],
                    "dlp_mgy_cm": None,
                }
                for rec in sorted(series.values(), key=lambda r: (r["series_number"] is None, r["series_number"]))
            ],
            "image_quality": None,
            "notes": notes,
        }

    # ------------------------------------------------------------------

    def _match_indication(
        self, protocol_name: str, study_description: str, body_part: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
        haystacks = {
            "protocol_name": protocol_name.lower(),
            "study_description": study_description.lower(),
            "body_part_examined": body_part.lower(),
        }
        for rule in self.indication_rules:
            matched_any = False
            ok = True
            for key in ("protocol_name_contains", "study_description_contains", "body_part_examined_contains"):
                needle = rule.get(key)
                if not needle:
                    continue
                field = key.replace("_contains", "")
                if needle.lower() not in haystacks.get(field, ""):
                    ok = False
                    break
                matched_any = True
            if ok and matched_any:
                return (
                    rule.get("region"),
                    rule.get("clinical_indication"),
                    rule.get("iv_contrast"),
                    True,
                )
        return None, None, None, False

    def _determine_bucket(
        self, region: Optional[str], indication: Optional[str],
        age_years: Optional[float], weight_kg: Optional[float],
    ) -> Tuple[Optional[str], Optional[str], List[str]]:
        notes: List[str] = []
        if not region or not indication:
            return None, None, ["protocol_type/examination_group left null: indication unresolved"]

        is_head_tab = (region, indication) in HEAD_INDICATIONS

        # Heuristic: PixieVeil has no reliable signal for "young adult" beyond
        # size/age thresholds, since RHYTHM's own GUI never auto-selects that
        # tab either — a technologist always picks it manually. Treat it as a
        # suggestion, not a decision.
        if weight_kg is not None and weight_kg > 80:
            notes.append("protocol_type=YOUNG_ADULT is a heuristic (weight > 80 kg) — verify")
            return "YOUNG_ADULT", YOUNG_ADULT_GROUP, notes
        if age_years is not None and age_years >= 18:
            notes.append("protocol_type=YOUNG_ADULT is a heuristic (age >= 18) — verify")
            return "YOUNG_ADULT", YOUNG_ADULT_GROUP, notes

        if is_head_tab:
            if age_years is None:
                return "PEDIATRIC_HEAD", None, notes + ["examination_group left null: age unknown"]
            for upper, group in HEAD_AGE_GROUPS:
                if age_years < upper:
                    return "PEDIATRIC_HEAD", group, notes
        else:
            if weight_kg is None:
                return "PEDIATRIC_BODY", None, notes + ["examination_group left null: weight unknown"]
            for upper, group in BODY_WEIGHT_GROUPS:
                if weight_kg < upper:
                    return "PEDIATRIC_BODY", group, notes

        return None, None, notes  # unreachable, satisfies type checkers
