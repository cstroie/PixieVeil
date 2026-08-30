"""Validate our built RHYTHM manifest items/documents against RHYTHM's own
JSON Schema, loaded from the vendored `reference/uploader` submodule
(`ct_upload_platform/uploads/manifest_schema.py`, pinned at fac5c52).

This is the check that makes vendoring that submodule pay off: if a later
`git submodule update --remote reference/uploader` changes RHYTHM's schema
in a way our builder or ported code tables no longer satisfy, this fails
with a field-level reason instead of us finding out from a 400 at upload
time. Skips cleanly when the submodule isn't checked out.
"""

import importlib.util
from pathlib import Path

import pytest

from pixieveil.processing import rhythm_manifest as rm
from .test_rhythm_manifest import _effective_sidecar


_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "reference" / "uploader" / "ct_upload_platform" / "uploads" / "manifest_schema.py"
)


@pytest.fixture(scope="module")
def vendored():
    """The vendored manifest_schema module, loaded straight from the
    submodule file (it's Django-free — only needs jsonschema)."""
    if not _SCHEMA_PATH.exists():
        pytest.skip("reference/uploader submodule not checked out")
    pytest.importorskip("jsonschema")
    spec = importlib.util.spec_from_file_location("_rhythm_schema_vendored", _SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _full_item():
    return rm.build_manifest_item(
        _effective_sidecar(),
        study_number=42,
        zip_filename="0042.zip",
        site_code="S001",
        sha256="a" * 64,
        size_bytes=180_000,
    )


class TestDrift:
    def test_type_discriminator_matches(self, vendored):
        assert rm.MANIFEST_TYPE == vendored.MANIFEST_TYPE_V2

    def test_required_item_fields_match_schema(self, vendored):
        schema_required = set(
            vendored.MANIFEST_SCHEMA_V2["properties"]["items"]["items"]["required"]
        )
        assert set(rm.REQUIRED_ITEM_FIELDS) == schema_required


class TestItemAgainstSchema:
    def test_full_item_is_schema_valid(self, vendored):
        assert vendored.validate_manifest_v2_item(_full_item()) == []

    def test_item_without_optional_strings_is_valid(self, vendored):
        sidecar = _effective_sidecar()
        sidecar["scanner"]["rhythm_scanner_id"] = None
        sidecar["protocol_used_hint"]["protocol_name"] = None
        item = rm.build_manifest_item(
            sidecar, study_number=1, zip_filename="x.zip", site_code="S001"
        )
        assert vendored.validate_manifest_v2_item(item) == []

    def test_null_required_field_is_rejected_by_schema(self, vendored):
        item = rm.build_manifest_item(
            _effective_sidecar(image_quality=None),
            study_number=1, zip_filename="x.zip", site_code="S001",
        )
        errors = vendored.validate_manifest_v2_item(item)
        assert any("image_quality" in e["field"] for e in errors)

    def test_bad_sha256_is_rejected_by_schema(self, vendored):
        item = _full_item()
        item["sha256"] = "not-a-hash"
        errors = vendored.validate_manifest_v2_item(item)
        assert any(e["field"].endswith("sha256") for e in errors)

    def test_nullable_numeric_fields_accepted_as_null(self, vendored):
        sidecar = _effective_sidecar(rdsr=None)
        sidecar["series"] = []
        sidecar["patient"] = {"age_years": None, "weight_kg": None}
        item = rm.build_manifest_item(
            sidecar, study_number=1, zip_filename="x.zip", site_code="S001"
        )
        assert vendored.validate_manifest_v2_item(item) == []


class TestBatchAgainstSchema:
    def test_built_batch_manifest_is_schema_valid(self, vendored):
        manifest = rm.build_batch_manifest(
            [_full_item()], batch="S001-20260830-120000", site_code="S001",
            note="pixieveil export",
        )
        assert vendored.validate_manifest_v2(manifest) == []

    def test_preview_blockers_key_does_not_break_v2_validation(self, vendored):
        # The /rhythm-manifest JSON view adds a top-level `blockers` key;
        # RHYTHM's v2 root schema is additionalProperties:true, so a file
        # that still carries it must validate.
        manifest = rm.build_batch_manifest(
            [_full_item()], batch="b", site_code="S001"
        )
        manifest["blockers"] = {"0053": ["site_code is not set"]}
        assert vendored.validate_manifest_v2(manifest) == []

    def test_batch_with_duplicate_ref_is_rejected(self, vendored):
        item = _full_item()
        manifest = rm.build_batch_manifest(
            [item, dict(item)], batch="b", site_code="S001"
        )
        errors = vendored.validate_manifest_v2(manifest)
        assert any(e["code"] in ("duplicate_ref", "duplicate_filename") for e in errors)
