"""Unit tests for the RHYTHM v2 manifest item builder (pure logic, no I/O)."""

from pixieveil.processing import rhythm_manifest as rm


_PROTOCOL_UUID = "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f"


def _effective_sidecar(**overrides):
    """A fully-resolved effective exam sidecar (post exam_merge.apply_manual),
    i.e. every field RHYTHM needs is present. Tests knock out individual
    pieces via *overrides* on the nested dicts.

    The ``indication`` / ``protocol_type`` / ``examination_group`` /
    ``scanner`` keys are still carried (the dashboard shows them and the
    sidecar's own bucket logic uses them) but no longer feed the manifest —
    RHYTHM derives all of that from the registered protocol behind
    ``protocol_id``.
    """
    data = {
        "study_number": 42,
        "indication": {
            "region": "Head",
            "clinical_indication": "Trauma",
            "iv_contrast": "Non-contrast",
            "matched": True,
        },
        "protocol_type": "PEDIATRIC_HEAD",
        "examination_group": "Group 4 - Childhood",
        "scanner": {
            "manufacturer": "SIEMENS",
            "model": "Emotion 16",
        },
        "rhythm": {"protocol_key": "head-trauma-routine"},
        "patient": {"age_years": 6, "weight_kg": 21.0},
        "rdsr": {
            "total_dlp_mgy_cm": 824.14,
            "acquisitions": [
                {"mean_ctdivol_mgy": None},
                {"mean_ctdivol_mgy": 12.3},
                {"mean_ctdivol_mgy": 40.1},
            ],
        },
        "series": [
            {"series_number": 2, "dlp_mgy_cm": 100.0, "ctdi_vol_mgy": 999.0},
        ],
        "image_quality": "Acceptable",
        "notes": [],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return data


class TestStudyDose:
    def test_prefers_rdsr_total_dlp_and_max_acq_ctdivol(self):
        ctdivol, dlp = rm._study_dose(_effective_sidecar())
        assert dlp == 824.14
        assert ctdivol == 40.1

    def test_falls_back_to_summed_series_dlp_estimates(self):
        sidecar = _effective_sidecar(rdsr=None)
        sidecar["series"] = [
            {"series_number": 2, "dlp_mgy_cm": 100.0},
            {"series_number": 3, "dlp_mgy_cm": 55.5},
            {"series_number": 4, "dlp_mgy_cm": None},
        ]
        ctdivol, dlp = rm._study_dose(sidecar)
        assert dlp == 155.5
        assert ctdivol is None  # never taken from the cumulative series tag

    def test_no_dose_data_at_all(self):
        sidecar = _effective_sidecar(rdsr=None)
        sidecar["series"] = []
        assert rm._study_dose(sidecar) == (None, None)


class TestBuildManifestItem:
    def test_happy_path_full_item(self):
        item = rm.build_manifest_item(
            _effective_sidecar(),
            study_number=42,
            zip_filename="0042_anonymized.zip",
            site_code="S001",
            protocol_id=_PROTOCOL_UUID,
            sha256="a" * 64,
            size_bytes=123456,
        )
        assert item["ref"] == "ROW0042"
        assert item["filename"] == "0042_anonymized.zip"
        assert item["site_code"] == "S001"
        assert item["protocol_id"] == _PROTOCOL_UUID
        assert item["image_quality"] == "Acceptable"
        assert item["patient_weight_kg"] == 21.0
        assert item["patient_age_years"] == 6
        assert item["ctdivol_mgy"] == 40.1
        assert item["dlp_mgy_cm"] == 824.14
        assert item["sha256"] == "a" * 64
        assert item["size_bytes"] == 123456
        assert rm.manifest_item_blockers(item) == []

    def test_retain_after_project_omitted_by_default(self):
        item = rm.build_manifest_item(
            _effective_sidecar(), study_number=1, zip_filename="x.zip",
            site_code="S001", protocol_id=_PROTOCOL_UUID,
        )
        assert "retain_after_project" not in item

    def test_retain_after_project_emitted_when_set(self):
        for value in (True, False):
            sidecar = _effective_sidecar()
            sidecar["rhythm"] = {**(sidecar.get("rhythm") or {}), "retain_after_project": value}
            item = rm.build_manifest_item(
                sidecar, study_number=1, zip_filename="x.zip",
                site_code="S001", protocol_id=_PROTOCOL_UUID,
            )
            assert item["retain_after_project"] is value

    def test_no_coded_fields_emitted(self):
        item = rm.build_manifest_item(
            _effective_sidecar(),
            study_number=1, zip_filename="x.zip",
            site_code="S001", protocol_id=_PROTOCOL_UUID,
        )
        for gone in (
            "clinical_indication_code", "anatomical_region", "contrast_code",
            "patient_group_code", "scanner_id", "protocol_name",
        ):
            assert gone not in item

    def test_checksum_fields_omitted_until_supplied(self):
        item = rm.build_manifest_item(
            _effective_sidecar(),
            study_number=1, zip_filename="x.zip",
            site_code="S001", protocol_id=_PROTOCOL_UUID,
        )
        assert "sha256" not in item
        assert "size_bytes" not in item

    def test_nullable_numeric_fields_kept_as_null(self):
        sidecar = _effective_sidecar(rdsr=None)
        sidecar["series"] = []
        sidecar["patient"] = {"age_years": None, "weight_kg": None}
        item = rm.build_manifest_item(
            sidecar, study_number=1, zip_filename="x.zip",
            site_code="S001", protocol_id=_PROTOCOL_UUID,
        )
        assert item["patient_weight_kg"] is None
        assert item["patient_age_years"] is None
        assert item["ctdivol_mgy"] is None
        assert item["dlp_mgy_cm"] is None


class TestManifestItemBlockers:
    def test_unset_image_quality_is_a_blocker(self):
        item = rm.build_manifest_item(
            _effective_sidecar(image_quality=None),
            study_number=1, zip_filename="x.zip",
            site_code="S001", protocol_id=_PROTOCOL_UUID,
        )
        assert rm.manifest_item_blockers(item) == ["image_quality is not set"]

    def test_unset_protocol_id_and_site_code_are_blockers(self):
        item = rm.build_manifest_item(
            _effective_sidecar(),
            study_number=1, zip_filename="x.zip",
            site_code=None, protocol_id=None,
        )
        blockers = rm.manifest_item_blockers(item)
        assert "protocol_id is not set" in blockers
        assert "site_code is not set" in blockers

    def test_blank_string_counts_as_unset(self):
        assert "site_code is not set" in rm.manifest_item_blockers({"site_code": "   "})


class TestBuildBatchManifest:
    def test_wraps_items_with_required_discriminators(self):
        item = rm.build_manifest_item(
            _effective_sidecar(),
            study_number=42,
            zip_filename="0042.zip",
            site_code="S001",
            protocol_id=_PROTOCOL_UUID,
            sha256="b" * 64,
            size_bytes=10,
        )
        manifest = rm.build_batch_manifest(
            [item], batch="S001-2026-08-30-001", site_code="S001", note="pixieveil export"
        )
        assert manifest["v"] == "1.0"
        assert manifest["type"] == "rhythm_server_assigned_upload_manifest"
        assert manifest["server_assigns_repo_id"] is True
        assert manifest["site"] == "S001"
        assert manifest["batch"] == "S001-2026-08-30-001"
        assert manifest["tool"] == "pixieveil"
        assert manifest["tool_version"]
        assert manifest["note"] == "pixieveil export"
        assert manifest["items"] == [item]

    def test_note_omitted_when_not_given(self):
        manifest = rm.build_batch_manifest([], batch="b", site_code="S001")
        assert "note" not in manifest
