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

**protocol_id, not coded fields** — RHYTHM v2 no longer carries per-item
``clinical_indication_code`` / ``anatomical_region`` / ``contrast_code`` /
``patient_group_code`` / ``scanner_id`` / ``protocol_name``. Instead an item
names one ``protocol_id`` — the UUID of a ``CTProtocol`` already registered
in the RHYTHM app (``/protocols/records/``) — and the server derives
anatomical region, clinical indication, contrast, protocol type, examination
group and scanner from that protocol. PixieVeil resolves the UUID from
``config/rhythm_protocols.yaml`` (see ``rhythm_protocols.ProtocolMap``),
keyed by the local protocol id the operator picks for the study on the
``/studies`` page. ``site_code`` on the item must match (case-insensitively)
the registered protocol's own ``site_code`` or RHYTHM fails the job.

Upstream sources, pinned:

    reference/uploader/ct_upload_platform/uploads/manifest_schema.py @ f4120f5
    reference/uploader/ct_upload_platform/uploads/tasks.py           @ f4120f5

Re-check ``REQUIRED_ITEM_FIELDS`` / the item shape against those on every
``git submodule update --remote reference/uploader`` —
``tests/test_rhythm_manifest_schema.py`` fails loudly if they drift.
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

# items[] entries RHYTHM's v2 schema marks required
# (MANIFEST_SCHEMA_V2["properties"]["items"]["items"]["required"]). A manifest
# item missing any of these (or carrying it as null/empty) is rejected at init.
REQUIRED_ITEM_FIELDS = (
    "ref",
    "filename",
    "site_code",
    "protocol_id",
    "image_quality",
)


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
    site_code: str | None,
    protocol_id: str | None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    """Build one RHYTHM v2 ``items[]`` entry from an *effective* exam sidecar
    (i.e. after ``exam_merge.apply_manual``), for the study's export ZIP.

    ``protocol_id`` is the UUID resolved from ``config/rhythm_protocols.yaml``
    for the local protocol the operator selected; ``site_code`` is the
    configured submitting site (``settings.storage.remote_storage.rhythm``).
    Either may be ``None`` here — ``manifest_item_blockers`` reports that as a
    blocker before upload. ``sha256`` / ``size_bytes`` describe the ZIP; pass
    them once the archive exists (the transport layer computes them).
    """
    patient = sidecar.get("patient") or {}
    ctdivol, dlp = _study_dose(sidecar)

    item: dict[str, Any] = {
        "ref": f"ROW{study_number:04d}",
        "filename": zip_filename,
        "site_code": site_code,
        "protocol_id": protocol_id,
        "image_quality": sidecar.get("image_quality"),
        "patient_weight_kg": patient.get("weight_kg"),
        "patient_age_years": patient.get("age_years"),
        "ctdivol_mgy": ctdivol,
        "dlp_mgy_cm": dlp,
    }

    # Retention consent (RHYTHM schema, reference/uploader 2d1dd43): only
    # emitted when the operator set it — omitted means RHYTHM's default
    # (kept after the project ends). False = delete at end of the Grant.
    retain = (sidecar.get("rhythm") or {}).get("retain_after_project")
    if isinstance(retain, bool):
        item["retain_after_project"] = retain

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
    site_code: str | None,
    note: str | None = None,
) -> dict[str, Any]:
    """Assemble the full v2 batch manifest document around *items*.

    RHYTHM has no manifest-ingest endpoint, so PixieVeil's own upload path
    passes ``items[]`` one at a time to ``chunked/init``. But the assembled
    document is written to ``export_path/<NNNN>.json`` as the shippable
    record — partner tooling that watches that directory submits it as-is —
    so ``batch`` / ``site_code`` must be real (``site_code`` may be ``None``
    when RHYTHM is unconfigured; the manifest then fails validation loudly
    rather than carrying a placeholder).
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
