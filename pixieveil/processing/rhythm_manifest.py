"""
RHYTHM v2 "server-assigned batch" upload manifest — item builder.

There is no RHYTHM endpoint that ingests a whole manifest file. The partner
upload GUI reads the manifest client-side and fans out one *chunked upload
per ``items[]`` entry*, tagging each with a shared ``batch`` string and that
one ``manifest_item`` dict (schema-checked at ``/api/v1/uploads/chunked/init/``).
So PixieVeil never writes or POSTs ``rhythm_upload_manifest.json`` as a wire
payload — it builds one item dict per study and passes it at ``init``.
``build_batch_manifest()`` still assembles the full document, but only so we
can drop a copy next to the retained study for our own audit trail.

The coded fields (``clinical_indication_code``, ``contrast_code``,
``patient_group_code``) are computed here from the human-readable values the
exam sidecar already carries, using tables ported from the RHYTHM server so
the items it receives need no further translation. Upstream sources, pinned:

    reference/uploader/ct_upload_platform/uploads/repository_study_id.py @ fac5c52
    reference/uploader/ct_upload_platform/uploads/manifest_schema.py     @ fac5c52
    reference/uploader/ct_upload_platform/uploads/tasks.py               @ fac5c52

Re-check these against the ported constants below on every
``git submodule update --remote reference/uploader``.
"""

import logging
from typing import Any

from pixieveil import __version__

logger = logging.getLogger(__name__)

# Discriminator + format version the RHYTHM v2 schema requires verbatim
# (manifest_schema.MANIFEST_TYPE_V2 / the "v" property).
MANIFEST_TYPE = "rhythm_server_assigned_upload_manifest"
MANIFEST_FORMAT_VERSION = "1.0"
TOOL_NAME = "pixieveil"

# (anatomical_region, clinical_indication) -> RHYTHM clinical_indication_code.
# The keys are exactly the strings ExamExtractor emits under
# indication.region / indication.clinical_indication, which the operator is
# instructed to keep aligned with RHYTHM's fixed 8-row vocabulary
# (integrations/rhythm/*_protocol.yaml). The codes mirror
# repository_study_id.INDICATION_CODES.
INDICATION_CODES: dict[tuple[str, str], str] = {
    ("Head", "Trauma"): "HEADTRAUMA",
    (
        "Mastoid bone/Inner Ear",
        "Hearing loss; congenital malformations, infection, cholesteatoma, "
        "cochlear implants",
    ): "MASTOID",
    ("Chest", "Complicated infections"): "CHESTCOMP",
    ("Chest", "Fungal infections"): "CHESTFUNG",
    (
        "Chest/HRCT (Inspiration/Expiration)",
        "Interstitial lung diseases, small airways disease, cystic fibrosis, "
        "asthma, primary ciliary dyskinesia, chronic lung disease of prematurity",
    ): "HRCTILD",
    ("Abdomen", "Acute abdomen"): "ACUTEABD",
    ("Neck-Chest-Abdomen", "Lymphoma"): "LYMPHOMA",
    (
        "Chest-Abdomen",
        "Tumor staging & follow-up (Wilms tumor, neuroblastoma, other)",
    ): "CHESTABD",
}

# iv_contrast string -> RHYTHM contrast_code. Mirrors
# repository_study_id.CONTRAST_CODES. ExamExtractor emits "Non-contrast" or
# "Contrast-enhanced"; an indication_lookup rule may additionally supply the
# combined "Non-contrast, Contrast-enhanced" (MIX).
CONTRAST_CODES: dict[str, str] = {
    "Non-contrast": "NC",
    "Contrast-enhanced": "CE",
    "Non-contrast, Contrast-enhanced": "MIX",
}

# Substring found in examination_group -> patient-group code, per
# protocol_type. Mirrors repository_study_id.get_patient_group_code /
# _PEDIATRIC_HEAD_GROUPS / _PEDIATRIC_BODY_GROUPS.
_PEDIATRIC_HEAD_GROUPS: dict[str, str] = {
    "Group 1": "PH-G1",
    "Group 2": "PH-G2",
    "Group 3": "PH-G3",
    "Group 4": "PH-G4",
}
_PEDIATRIC_BODY_GROUPS: dict[str, str] = {
    "Group 1": "PB-G1",
    "Group 2": "PB-G2",
    "Group 3": "PB-G3",
    "Group 4": "PB-G4",
    "Group 5": "PB-G5",
}

# items[] entries RHYTHM's v2 schema marks required. A manifest item missing
# any of these (or carrying it as null/empty) is rejected at init.
REQUIRED_ITEM_FIELDS = (
    "ref",
    "filename",
    "site_code",
    "clinical_indication_code",
    "anatomical_region",
    "contrast_code",
    "patient_group_code",
    "image_quality",
)


def indication_code(region: str | None, clinical_indication: str | None) -> str | None:
    """Resolve (region, clinical_indication) to a RHYTHM clinical_indication_code,
    or None when the pair is not one of the curated 8."""
    if not region or not clinical_indication:
        return None
    return INDICATION_CODES.get((region, clinical_indication))


def contrast_code(iv_contrast: str | None) -> str | None:
    """Resolve an iv_contrast string to NC / CE / MIX, or None if unrecognised."""
    if not iv_contrast:
        return None
    return CONTRAST_CODES.get(iv_contrast)


def patient_group_code(
    protocol_type: str | None, examination_group: str | None
) -> str | None:
    """Derive the RHYTHM patient_group_code from the sidecar's protocol_type /
    examination_group buckets.

    Returns None (not repository_study_id's "UNK" sentinel) when the bucket is
    unresolved, so ``manifest_item_blockers`` treats it as a hard gap rather
    than shipping an un-assignable code.
    """
    if protocol_type == "YOUNG_ADULT":
        return "YA-G6"
    group = examination_group or ""
    if protocol_type == "PEDIATRIC_HEAD":
        for needle, code in _PEDIATRIC_HEAD_GROUPS.items():
            if needle in group:
                return code
    elif protocol_type == "PEDIATRIC_BODY":
        for needle, code in _PEDIATRIC_BODY_GROUPS.items():
            if needle in group:
                return code
    return None


def _study_dose(sidecar: dict[str, Any]) -> tuple[float | None, float | None]:
    """Best-effort (ctdivol_mgy, dlp_mgy_cm) for the whole studyset.

    Both are optional in the RHYTHM item schema and the server prefers its own
    re-parsed RDSR, so this stays conservative:

      * dlp  — ``rdsr.total_dlp_mgy_cm`` when an RDSR/hybrid was parsed;
               otherwise the sum of the per-series ``dlp_mgy_cm`` estimates
               (CTDIvol x imaged length), or None if none are known.
      * ctdivol — the largest ``rdsr.acquisitions[*].mean_ctdivol_mgy``.
               Deliberately NOT falling back to ``series[*].ctdi_vol_mgy``:
               on our scanner that flat tag is cumulative dose-to-date, not a
               per-acquisition value, so it would overstate the dose.
    """
    rdsr = sidecar.get("rdsr")
    ctdivol: float | None = None
    dlp: float | None = None

    if isinstance(rdsr, dict):
        dlp = rdsr.get("total_dlp_mgy_cm")
        acq_ctdis = [
            a.get("mean_ctdivol_mgy")
            for a in rdsr.get("acquisitions") or []
            if isinstance(a, dict) and a.get("mean_ctdivol_mgy") is not None
        ]
        if acq_ctdis:
            ctdivol = max(acq_ctdis)

    if dlp is None:
        series_dlps = [
            s.get("dlp_mgy_cm")
            for s in sidecar.get("series") or []
            if isinstance(s, dict) and s.get("dlp_mgy_cm") is not None
        ]
        if series_dlps:
            dlp = round(sum(series_dlps), 2)

    return ctdivol, dlp


def build_manifest_item(
    sidecar: dict[str, Any],
    *,
    study_number: int,
    zip_filename: str,
    site_code: str,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    """Build one RHYTHM v2 ``items[]`` entry from an *effective* exam sidecar
    (i.e. after ``exam_merge.apply_manual``), for the study's export ZIP.

    ``sha256`` / ``size_bytes`` describe that ZIP; pass them once the archive
    exists (the transport layer computes them). The item may still be missing
    required fields — call ``manifest_item_blockers`` before uploading it.
    """
    indication = sidecar.get("indication") or {}
    patient = sidecar.get("patient") or {}
    scanner = sidecar.get("scanner") or {}
    hint = sidecar.get("protocol_used_hint") or {}

    region = indication.get("region")
    ctdivol, dlp = _study_dose(sidecar)

    item: dict[str, Any] = {
        "ref": f"ROW{study_number:04d}",
        "filename": zip_filename,
        "site_code": site_code,
        "clinical_indication_code": indication_code(
            region, indication.get("clinical_indication")
        ),
        "anatomical_region": region,
        "contrast_code": contrast_code(indication.get("iv_contrast")),
        "patient_group_code": patient_group_code(
            sidecar.get("protocol_type"), sidecar.get("examination_group")
        ),
        "image_quality": sidecar.get("image_quality"),
        "patient_weight_kg": patient.get("weight_kg"),
        "patient_age_years": patient.get("age_years"),
        "ctdivol_mgy": ctdivol,
        "dlp_mgy_cm": dlp,
    }

    # Optional string fields: the schema types them as plain "string" (not
    # nullable), so omit rather than send null.
    scanner_id = scanner.get("rhythm_scanner_id")
    if scanner_id:
        item["scanner_id"] = scanner_id
    protocol_name = hint.get("protocol_name")
    if protocol_name:
        item["protocol_name"] = protocol_name

    if sha256 is not None:
        item["sha256"] = sha256
    if size_bytes is not None:
        item["size_bytes"] = size_bytes

    return item


def manifest_item_blockers(item: dict[str, Any]) -> list[str]:
    """Return human-readable reasons *item* cannot be uploaded yet — one per
    required field that is missing, null, or blank. Empty list means the item
    satisfies RHYTHM's required-field set (the server still runs full schema
    and GDPR validation on top).
    """
    blockers: list[str] = []
    for field in REQUIRED_ITEM_FIELDS:
        value = item.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            blockers.append(f"{field} is not set")
    return blockers


def build_batch_manifest(
    items: list[dict[str, Any]],
    *,
    batch: str,
    site_code: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Assemble the full v2 batch manifest document around *items*.

    Not sent on the wire (RHYTHM has no manifest-ingest endpoint) — written
    beside the retained study as an audit record mirroring the partner tool's
    ``rhythm_upload_manifest.json`` output.
    """
    manifest: dict[str, Any] = {
        "v": MANIFEST_FORMAT_VERSION,
        "type": MANIFEST_TYPE,
        "server_assigns_repo_id": True,
        "site": site_code,
        "batch": batch,
        "tool": TOOL_NAME,
        "tool_version": __version__,
        "items": items,
    }
    if note:
        manifest["note"] = note
    return manifest
