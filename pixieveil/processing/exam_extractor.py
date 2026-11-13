"""
RHYTHM exam-data extraction.

Reads the (already anonymized) DICOM files of a completed study and derives
as many fields as possible for the RHYTHM "Manual Exam Entry" form
(see integrations/rhythm/). Fields DICOM cannot answer — image quality,
which saved protocol was followed, DLP without an RDSR object, etc. — are
left ``None`` with an explanatory entry in ``notes`` rather than guessed.

Scanner, protocol, RHYTHM protocol key and region/indication/contrast come
from the scanners & protocols catalog (``config/protocols.yaml``, edited on
the dashboard's ``/protocols`` page — see ``protocol_catalog``): the study's
(Manufacturer, Model) picks the scanner, its ProtocolName (or the protocol
part of StudyDescription) picks the protocol by exact name. An unmatched
study keeps those fields null with a ``Protocol catalog:`` note.

Classes:
    ExamExtractor: Reads a study directory and builds the exam-data dict
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pydicom

from pixieveil.processing import exam_merge
from pixieveil.processing.protocol_catalog import ProtocolCatalog

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


def _first_float(rec: Dict[str, Any], key: str, raw: Any) -> None:
    """Set rec[key] from raw if it's currently unset and raw parses as a float."""
    if rec.get(key) is not None or raw in (None, ""):
        return
    try:
        rec[key] = float(raw)
    except (TypeError, ValueError):
        pass


def _first_str(rec: Dict[str, Any], key: str, raw: Any) -> None:
    """Set rec[key] from raw if it's currently unset. Joins multi-valued
    DICOM elements (e.g. ConvolutionKernel can report several values) with
    '/' rather than keeping only the first."""
    if rec.get(key) is not None or raw in (None, ""):
        return
    if isinstance(raw, str):
        value = raw
    elif hasattr(raw, "__iter__"):
        # pydicom.multival.MultiValue (e.g. a multi-valued ConvolutionKernel)
        # is iterable but not a list/tuple subclass.
        value = "/".join(str(v) for v in raw)
    else:
        value = str(raw)
    if value:
        rec[key] = value


def _estimate_series_dlp(rec: Dict[str, Any]) -> Optional[float]:
    """
    Estimate a series' DLP as CTDIvol × imaged length, when no RDSR value is
    available for it. Length is the z-extent spanned by ImagePositionPatient
    across the series' images, padded by one slice thickness to cover the
    two end slices (each contributes a full slice, not just its center
    point) — the same convention scanners themselves use to report DLP.
    This is an estimate, not a measurement: gantry tilt, a table pitch mid-
    series, or an incomplete/re-queued series would all skew it. Skipped for
    a topogram — it's a single fast low-dose sweep, not a rotational
    acquisition, so CTDIvol × length isn't a meaningful relationship for it.
    """
    if rec.get("is_topogram"):
        return None
    ctdi = rec["ctdi_vol_mgy"]
    if ctdi is None:
        return None
    z_min, z_max = rec["z_min_mm"], rec["z_max_mm"]
    thickness = rec["slice_thickness_mm"] or 0.0
    if z_min is not None and z_max is not None and z_max > z_min:
        length_mm = (z_max - z_min) + thickness
    elif thickness:
        length_mm = thickness  # single-image series
    else:
        return None
    return round(ctdi * (length_mm / 10.0), 2)


# DCM/SRT concept codes used by the CT Radiation Dose SR template (TID 10011
# "X-Ray Radiation Dose Report" / TID 10013 "CT Acquisition"). Some scanners
# (confirmed on our Siemens Emotion 16) send this as a *Secondary Capture*
# object — SOPClassUID 1.2.840.10008.5.1.4.1.1.7, not the real RDSR SOP class
# — with the same structured content tree attached alongside the rendered
# screenshot pixels. _parse_rdsr_content reads that tree wherever it's found,
# regardless of which SOP class carries it.
_RDSR_ROOT = "113701"                    # X-Ray Radiation Dose Report
_RDSR_TOTAL_DLP = "113813"               # CT Dose Length Product Total
_RDSR_ACQUISITION = "113819"             # CT Acquisition (container, repeated)
_RDSR_ACQ_PROTOCOL = "125203"            # Acquisition Protocol
_RDSR_TARGET_REGION = "123014"           # Target Region
_RDSR_ACQ_TYPE = "113820"                # CT Acquisition Type (Spiral/Sequenced/...)
_RDSR_PROCEDURE_CONTEXT = "G-C32C"       # Procedure Context (e.g. "CT without contrast")
_RDSR_PITCH = "113828"                   # Pitch Factor
_RDSR_KVP = "113733"                     # KVP
_RDSR_MAX_MA = "113833"                  # Maximum X-Ray Tube Current
_RDSR_MEAN_MA = "113734"                 # Mean X-Ray Tube Current
_RDSR_ROTATION_TIME = "113834"           # Exposure Time per Rotation
_RDSR_MEAN_CTDIVOL = "113830"            # Mean CTDIvol
_RDSR_DLP = "113838"                     # DLP (per acquisition)
_RDSR_MODULATION_TYPE = "113842"         # X-Ray Modulation Type


def _sr_concept_code(item: "pydicom.Dataset") -> Optional[str]:
    seq = item.get("ConceptNameCodeSequence")
    if not seq:
        return None
    return str(getattr(seq[0], "CodeValue", "") or "") or None


def _sr_numeric_value(item: "pydicom.Dataset") -> Optional[float]:
    mv = item.get("MeasuredValueSequence")
    if not mv:
        return None
    raw = getattr(mv[0], "NumericValue", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _sr_text_or_code_value(item: "pydicom.Dataset") -> Optional[str]:
    value_type = str(getattr(item, "ValueType", "") or "")
    if value_type == "TEXT":
        return str(getattr(item, "TextValue", "") or "") or None
    if value_type == "CODE":
        ccs = item.get("ConceptCodeSequence")
        if ccs:
            return str(getattr(ccs[0], "CodeMeaning", "") or "") or None
    return None


def _collect_sr_subtree_by_code(item: "pydicom.Dataset") -> Dict[str, "pydicom.Dataset"]:
    """Flatten every descendant of item into {concept_code: item}, first hit wins."""
    found: Dict[str, "pydicom.Dataset"] = {}

    def walk(node: "pydicom.Dataset") -> None:
        for child in node.get("ContentSequence") or []:
            code = _sr_concept_code(child)
            if code and code not in found:
                found[code] = child
            walk(child)

    walk(item)
    return found


def _parse_rdsr_content(ds: "pydicom.Dataset") -> Optional[Dict[str, Any]]:
    """
    If ds carries an X-Ray Radiation Dose Report content tree (native RDSR or
    a vendor hybrid Secondary Capture with the same tree attached), parse it
    into {total_dlp_mgy_cm, acquisitions: [...]}. Returns None otherwise.
    """
    if _sr_concept_code(ds) != _RDSR_ROOT:
        return None

    top_level = _collect_sr_subtree_by_code(ds)
    total_dlp_item = top_level.get(_RDSR_TOTAL_DLP)
    total_dlp = _sr_numeric_value(total_dlp_item) if total_dlp_item is not None else None

    acquisitions = []
    for item in ds.get("ContentSequence") or []:
        acquisitions.extend(_find_acquisitions(item))

    return {
        "total_dlp_mgy_cm": total_dlp,
        "acquisitions": acquisitions,
    }


def _find_acquisitions(node: "pydicom.Dataset") -> List[Dict[str, Any]]:
    """Recursively find every 'CT Acquisition' container and parse it."""
    results = []
    if _sr_concept_code(node) == _RDSR_ACQUISITION:
        results.append(_parse_acquisition(node))
    for child in node.get("ContentSequence") or []:
        results.extend(_find_acquisitions(child))
    return results


def _parse_acquisition(acq_item: "pydicom.Dataset") -> Dict[str, Any]:
    by_code = _collect_sr_subtree_by_code(acq_item)

    def text_or_code(code: str) -> Optional[str]:
        item = by_code.get(code)
        return _sr_text_or_code_value(item) if item is not None else None

    def numeric(code: str) -> Optional[float]:
        item = by_code.get(code)
        return _sr_numeric_value(item) if item is not None else None

    return {
        "acquisition_protocol": text_or_code(_RDSR_ACQ_PROTOCOL),
        "target_region": text_or_code(_RDSR_TARGET_REGION),
        "acquisition_type": text_or_code(_RDSR_ACQ_TYPE),
        "procedure_context": text_or_code(_RDSR_PROCEDURE_CONTEXT),
        "pitch": numeric(_RDSR_PITCH),
        "kvp": numeric(_RDSR_KVP),
        "mean_ma": numeric(_RDSR_MEAN_MA),
        "max_ma": numeric(_RDSR_MAX_MA),
        "rotation_time_s": numeric(_RDSR_ROTATION_TIME),
        "mean_ctdivol_mgy": numeric(_RDSR_MEAN_CTDIVOL),
        "dlp_mgy_cm": numeric(_RDSR_DLP),
        "modulation_type": text_or_code(_RDSR_MODULATION_TYPE),
    }


def _determine_bucket(
    region: Optional[str], indication: Optional[str],
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


def _is_ct_image(ds: "pydicom.Dataset") -> bool:
    """
    True for anything acquired as CT — including a topogram/localizer, which
    is a real irradiation event and belongs in the series list with the rest
    of the dose record. False only for a non-CT object riding along in the
    same study directory, e.g. a rendered Dose Report screen capture (SC
    modality) — that one isn't a DICOM series of the exam at all.
    """
    modality = str(getattr(ds, "Modality", "") or "")
    return not modality or modality == "CT"


# Per-series CTDIvol is the max over a series' images; with tube-current
# modulation that sits well above the RDSR's per-acquisition *mean* (seen up
# to ~+16% on Siemens Emotion 16 / SOMATOM go.*), so "matches" is loose.
_CTDIVOL_TOLERANCE = 0.25


def _classify_per_image_ctdivol(series_ctdivols: List[float],
                                acq_ctdivols: List[float]) -> Optional[str]:
    """How a scanner's per-image CTDIvol tag relates to the RDSR record.

    Returns ``"per_acquisition"`` when no series exceeds the largest single
    acquisition (within tolerance), ``"cumulative"`` when every value that
    does exceed it matches a running total of the acquisitions, else
    ``"unclear"``; ``None`` when either side has no data.
    """
    acq = [a for a in acq_ctdivols if a is not None]
    vals = [v for v in series_ctdivols if v is not None]
    if not acq or not vals:
        return None
    tol = _CTDIVOL_TOLERANCE
    ceiling = max(acq) * (1 + tol)
    exceeding = [v for v in vals if v > ceiling]
    if not exceeding:
        return "per_acquisition"
    running, totals = 0.0, []
    for a in acq:
        running += a
        totals.append(running)
    if all(any(abs(v - t) <= tol * t for t in totals) for v in exceeding):
        return "cumulative"
    return "unclear"


_CTDIVOL_NOTES = {
    "per_acquisition": (
        "rdsr populated: per-image CTDIvol matches the RDSR per-acquisition values "
        "(series[*].ctdi_vol_mgy is the max over a series' images, so it can sit above "
        "the RDSR mean under tube-current modulation) — prefer "
        "rdsr.acquisitions[*].mean_ctdivol_mgy as the authoritative record"
    ),
    "cumulative": (
        "rdsr populated: per-image CTDIvol on this scanner reports the cumulative "
        "dose-to-date, not the per-acquisition value — use "
        "rdsr.acquisitions[*].mean_ctdivol_mgy, not series[*].ctdi_vol_mgy"
    ),
    "unclear": (
        "rdsr populated: per-image CTDIvol doesn't line up with the RDSR acquisitions "
        "(neither per-acquisition nor a running total) — prefer "
        "rdsr.acquisitions[*].mean_ctdivol_mgy"
    ),
    None: (
        "rdsr populated: prefer rdsr.acquisitions[*].mean_ctdivol_mgy over "
        "series[*].ctdi_vol_mgy"
    ),
}


def _is_topogram(ds: "pydicom.Dataset") -> bool:
    image_type = [str(v).upper() for v in (getattr(ds, "ImageType", None) or [])]
    return "LOCALIZER" in image_type


class ExamExtractor:
    """Derives RHYTHM exam-entry fields from a completed study's DICOM files."""

    def __init__(self, catalog: Optional[ProtocolCatalog] = None):
        # No catalog → nothing matches (every study gets a "map it" note).
        self.catalog = catalog if catalog is not None else ProtocolCatalog(
            Path("/nonexistent/protocols.yaml"), scanners=[])

    # ------------------------------------------------------------------

    def _scan_dicom_files(
        self, study_dir: Path, hints: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Read every anonymized .dcm under study_dir once, accumulating the raw
        study-level headers, patient age/weight, RDSR content, and per-series
        technique records that extract() turns into the exam-data dict.

        *hints* is the optional ``{"study": {...}, "series": {"<n>": {...}}}``
        dict StorageManager captured from the raw images before anonymization
        — it seeds ``manufacturer`` / ``model`` / ``protocol_name`` /
        ``study_description`` and per-series ``series_description`` so the
        scanner/indication lookups still resolve under the ``rhythm``
        anonymization profile, which strips those tags from the stored files.
        A real header value still wins over a hint when both are present.

        Keyed by DICOM SeriesNumber. NOT the same thing as an "irradiation
        event"/RDSR acquisition — one acquisition can be reconstructed into
        several series (different kernel/thickness), so several entries here
        can share one real dose event. See rdsr.acquisitions[] for the true
        per-irradiation-event record.
        """
        series_records: Dict[str, Dict[str, Any]] = {}
        manufacturer = model = None
        protocol_name = study_description = body_part = ""
        contrast_seen = False
        age_years: Optional[float] = None
        weight_kg: Optional[float] = None
        rdsr_data: Optional[Dict[str, Any]] = None

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

            if rdsr_data is None:
                rdsr_data = _parse_rdsr_content(ds)

            if not _is_ct_image(ds):
                continue

            series_number = getattr(ds, "SeriesNumber", None)
            key = str(series_number) if series_number is not None else "?"
            rec = series_records.setdefault(key, {
                # Local storage folder (<study>/<NNNN>/, numbered by arrival
                # order) — the key the pre-anonymization hints use. Not the
                # DICOM SeriesNumber; popped before the record is returned.
                "_folder": f.parent.name,
                "series_number": series_number,
                "series_description": str(getattr(ds, "SeriesDescription", "") or ""),
                "is_topogram": False,
                "ctdi_vol_mgy": None,
                "dlp_mgy_cm": None,
                "slice_thickness_mm": None,
                "exposure_time_ms": None,
                "convolution_kernel": None,
                "patient_position": None,
                "single_collimation_width_mm": None,
                "total_collimation_width_mm": None,
                "spiral_pitch_factor": None,
                "z_min_mm": None,
                "z_max_mm": None,
            })
            if _is_topogram(ds):
                rec["is_topogram"] = True
            ctdi = getattr(ds, "CTDIvol", None)
            if ctdi is not None:
                try:
                    ctdi = float(ctdi)
                    if rec["ctdi_vol_mgy"] is None or ctdi > rec["ctdi_vol_mgy"]:
                        rec["ctdi_vol_mgy"] = ctdi
                except (TypeError, ValueError):
                    pass

            # Track the series' imaged extent along the patient z-axis so a
            # per-series DLP can be estimated as CTDIvol × scan length when
            # no RDSR is available to report the real value directly.
            position = getattr(ds, "ImagePositionPatient", None)
            if position is not None and len(position) == 3:
                try:
                    z = float(position[2])
                    if rec["z_min_mm"] is None or z < rec["z_min_mm"]:
                        rec["z_min_mm"] = z
                    if rec["z_max_mm"] is None or z > rec["z_max_mm"]:
                        rec["z_max_mm"] = z
                except (TypeError, ValueError):
                    pass

            # Reconstruction/acquisition technique parameters — constant for
            # a given series (unlike CTDIvol, which climbs slice-by-slice on
            # this scanner), so the first value seen is kept rather than
            # maxed. Not part of the RDSR content tree (that only covers
            # dose parameters), so these come from the image headers only.
            _first_float(rec, "slice_thickness_mm", getattr(ds, "SliceThickness", None))
            _first_float(rec, "exposure_time_ms", getattr(ds, "ExposureTime", None))
            _first_str(rec, "convolution_kernel", getattr(ds, "ConvolutionKernel", None))
            _first_str(rec, "patient_position", getattr(ds, "PatientPosition", None))
            _first_float(
                rec, "single_collimation_width_mm", getattr(ds, "SingleCollimationWidth", None)
            )
            _first_float(
                rec, "total_collimation_width_mm", getattr(ds, "TotalCollimationWidth", None)
            )
            _first_float(rec, "spiral_pitch_factor", getattr(ds, "SpiralPitchFactor", None))

        # Fill anything the (possibly rhythm-anonymized) headers left blank
        # from the pre-anonymization hints — a real header value always wins.
        study_hints = (hints or {}).get("study", {})
        manufacturer = manufacturer or (study_hints.get("manufacturer") or None)
        model = model or (study_hints.get("model") or None)
        protocol_name = protocol_name or study_hints.get("protocol_name", "")
        study_description = study_description or study_hints.get("study_description", "")
        series_hints = (hints or {}).get("series", {})
        for rec in series_records.values():
            # Hints are keyed by local folder number (StorageManager assigns
            # those in arrival order), NOT DICOM SeriesNumber — matching on
            # SeriesNumber attached e.g. a recon's description to the
            # topogram whenever the scanner sent series out of order.
            folder = rec.pop("_folder", "")
            hkey = str(int(folder)) if folder.isdigit() else None
            if not rec["series_description"] and hkey is not None:
                rec["series_description"] = (
                    series_hints.get(hkey, {}).get("series_description", "")
                )

        return {
            "dcm_files": dcm_files,
            "manufacturer": manufacturer,
            "model": model,
            "protocol_name": protocol_name,
            "study_description": study_description,
            "body_part": body_part,
            "contrast_seen": contrast_seen,
            "age_years": age_years,
            "weight_kg": weight_kg,
            "rdsr_data": rdsr_data,
            "series_records": series_records,
        }

    @staticmethod
    def _build_series_list(
        series_records: Dict[str, Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Sort series_records by SeriesNumber and estimate a per-series DLP.

        Returns ``(series_list, any_estimated_dlp)`` — the latter drives the
        "series[*].dlp_mgy_cm is an estimate" note in extract().
        """
        sorted_records = sorted(
            series_records.values(), key=lambda r: (r["series_number"] is None, r["series_number"])
        )
        series_list = []
        any_estimated_dlp = False
        for rec in sorted_records:
            estimated_dlp = _estimate_series_dlp(rec)
            if estimated_dlp is not None:
                any_estimated_dlp = True
            series_list.append({
                "series_number": rec["series_number"],
                "series_description": rec["series_description"] or None,
                "is_topogram": rec["is_topogram"],
                "ctdi_vol_mgy": rec["ctdi_vol_mgy"],
                "dlp_mgy_cm": estimated_dlp,
                "slice_thickness_mm": rec["slice_thickness_mm"],
                "exposure_time_ms": rec["exposure_time_ms"],
                "convolution_kernel": rec["convolution_kernel"],
                "patient_position": rec["patient_position"],
                "single_collimation_width_mm": rec["single_collimation_width_mm"],
                "total_collimation_width_mm": rec["total_collimation_width_mm"],
                "spiral_pitch_factor": rec["spiral_pitch_factor"],
            })
        return series_list, any_estimated_dlp

    def extract(self, study_dir: Path, study_number: int,
                anonymized_study_uid: str,
                hints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Read every .dcm in study_dir and build the exam-data dict.

        *hints* — see ``_scan_dicom_files``; the pre-anonymization
        scanner/description values, needed when the study was anonymized
        under the ``rhythm`` profile.
        """
        notes: List[str] = []

        scanned = self._scan_dicom_files(study_dir, hints)
        manufacturer = scanned["manufacturer"]
        model = scanned["model"]
        protocol_name = scanned["protocol_name"]
        study_description = scanned["study_description"]
        # BodyPartExamined (scanned["body_part"]) no longer drives matching;
        # the catalog matches on scanner + protocol name.
        contrast_seen = scanned["contrast_seen"]
        age_years = scanned["age_years"]
        weight_kg = scanned["weight_kg"]
        rdsr_data = scanned["rdsr_data"]
        series_records = scanned["series_records"]

        if not scanned["dcm_files"]:
            notes.append("No .dcm files found under study_dir at extraction time")

        if weight_kg is None:
            notes.append("PatientWeight absent on all images — needs manual entry")
        if age_years is None:
            notes.append("PatientAge absent or unparseable — needs manual entry")

        if rdsr_data is None:
            notes.append(
                "rdsr null: no X-Ray Radiation Dose Report content found in this study "
                "(native RDSR or a vendor hybrid Secondary Capture carrying the same "
                "content tree); DLP needs to be read off the console dose screen manually"
            )
        else:
            # Derived from this study's own data rather than assumed per
            # scanner: compare per-series CTDIvol against the RDSR record.
            verdict = _classify_per_image_ctdivol(
                [rec["ctdi_vol_mgy"] for rec in series_records.values()],
                [a.get("mean_ctdivol_mgy") for a in rdsr_data.get("acquisitions", [])],
            )
            rdsr_data["per_image_ctdivol"] = verdict
            notes.append(_CTDIVOL_NOTES[verdict])

        match = self.catalog.match(manufacturer, model, protocol_name, study_description)
        proto = match.protocol
        indication_matched = bool(proto is not None and proto.has_indication)
        region = proto.region if indication_matched else None
        indication = proto.clinical_indication if indication_matched else None
        # Catalog contrast wins; otherwise infer from ContrastBolusAgent.
        contrast = (proto.iv_contrast if indication_matched and proto.iv_contrast
                    else ("Contrast-enhanced" if contrast_seen else "Non-contrast"))
        notes.extend(exam_merge.catalog_notes(match, manufacturer, model))

        protocol_type, examination_group, bucket_notes = _determine_bucket(
            region, indication, age_years, weight_kg
        )
        notes.extend(bucket_notes)

        series_list, any_estimated_dlp = self._build_series_list(series_records)
        if any_estimated_dlp:
            notes.append(
                "series[*].dlp_mgy_cm is an estimate (CTDIvol × imaged length "
                "from ImagePositionPatient, not a measured value) — prefer "
                "rdsr.acquisitions[*].dlp_mgy_cm when an RDSR is present"
            )

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
                **({"source": "catalog"} if indication_matched else {}),
            },
            "rdsr": rdsr_data,
            "protocol_type": protocol_type,
            "examination_group": examination_group,
            "scanner": {
                "manufacturer": manufacturer,
                "model": model,
                "rhythm_scanner_id": match.scanner.rhythm_scanner_id if match.scanner else None,
                "catalog_id": match.scanner.id if match.scanner else None,
                "name": match.scanner.label if match.scanner else None,
            },
            "rhythm": {"protocol_key": proto.key if proto else None},
            "catalog": {
                "scanner_id": match.scanner.id if match.scanner else None,
                "protocol_key": proto.key if proto else None,
                "protocol_name": match.protocol_name or None,
            },
            "protocol_used_hint": {
                "protocol_name": protocol_name or None,
                "study_description": study_description or None,
                "series_description_sample": next(
                    (r["series_description"] for r in series_records.values()
                     if r["series_description"]),
                    None,
                ),
            },
            "patient": {
                "age_years": age_years,
                "weight_kg": weight_kg,
            },
            "series": series_list,
            "image_quality": None,
            "notes": notes,
        }

    # ------------------------------------------------------------------
