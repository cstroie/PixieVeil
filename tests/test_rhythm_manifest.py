"""Unit tests for the RHYTHM v2 manifest item builder (pure logic, no I/O)."""

from pixieveil.processing import rhythm_manifest as rm


def _effective_sidecar(**overrides):
    """A fully-resolved effective exam sidecar (post exam_merge.apply_manual),
    i.e. every field RHYTHM needs is present. Tests knock out individual
    pieces via *overrides* on the nested dicts."""
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
            "rhythm_scanner_id": "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f",
        },
        "protocol_used_hint": {"protocol_name": "HeadRoutine"},
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


class TestIndicationCode:
    def test_all_eight_curated_pairs_resolve(self):
        assert len(rm.INDICATION_CODES) == 8
        for (region, indication), code in rm.INDICATION_CODES.items():
            assert rm.indication_code(region, indication) == code

    def test_unknown_pair_is_none(self):
        assert rm.indication_code("Head", "Tumour screening") is None

    def test_missing_half_is_none(self):
        assert rm.indication_code("Head", None) is None
        assert rm.indication_code(None, "Trauma") is None


class TestContrastCode:
    def test_known_values(self):
        assert rm.contrast_code("Non-contrast") == "NC"
        assert rm.contrast_code("Contrast-enhanced") == "CE"
        assert rm.contrast_code("Non-contrast, Contrast-enhanced") == "MIX"

    def test_unknown_or_missing(self):
        assert rm.contrast_code("with dye") is None
        assert rm.contrast_code(None) is None


class TestPatientGroupCode:
    def test_pediatric_head_group_substring_match(self):
        assert rm.patient_group_code("PEDIATRIC_HEAD", "Group 4 - Childhood") == "PH-G4"

    def test_pediatric_body_group_five(self):
        assert rm.patient_group_code("PEDIATRIC_BODY", "Group 5 - Adolescence") == "PB-G5"

    def test_young_adult_ignores_group_string(self):
        assert rm.patient_group_code("YOUNG_ADULT", None) == "YA-G6"
        assert rm.patient_group_code("YOUNG_ADULT", "anything") == "YA-G6"

    def test_pediatric_body_has_no_group_four_for_head_table(self):
        # PEDIATRIC_HEAD only defines Groups 1-4; a Group 5 string is unresolved.
        assert rm.patient_group_code("PEDIATRIC_HEAD", "Group 5") is None

    def test_unresolved_bucket_is_none_not_unk(self):
        assert rm.patient_group_code(None, None) is None
        assert rm.patient_group_code("PEDIATRIC_HEAD", "") is None


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
            sha256="a" * 64,
            size_bytes=123456,
        )
        assert item["ref"] == "ROW0042"
        assert item["filename"] == "0042_anonymized.zip"
        assert item["site_code"] == "S001"
        assert item["clinical_indication_code"] == "HEADTRAUMA"
        assert item["anatomical_region"] == "Head"
        assert item["contrast_code"] == "NC"
        assert item["patient_group_code"] == "PH-G4"
        assert item["image_quality"] == "Acceptable"
        assert item["patient_weight_kg"] == 21.0
        assert item["patient_age_years"] == 6
        assert item["ctdivol_mgy"] == 40.1
        assert item["dlp_mgy_cm"] == 824.14
        assert item["scanner_id"] == "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f"
        assert item["protocol_name"] == "HeadRoutine"
        assert item["sha256"] == "a" * 64
        assert item["size_bytes"] == 123456
        assert rm.manifest_item_blockers(item) == []

    def test_optional_string_fields_omitted_when_absent_not_nulled(self):
        sidecar = _effective_sidecar()
        sidecar["scanner"]["rhythm_scanner_id"] = None
        sidecar["protocol_used_hint"]["protocol_name"] = None
        item = rm.build_manifest_item(
            sidecar,
            study_number=1,
            zip_filename="x.zip",
            site_code="S001",
        )
        assert "scanner_id" not in item
        assert "protocol_name" not in item

    def test_checksum_fields_omitted_until_supplied(self):
        item = rm.build_manifest_item(
            _effective_sidecar(),
            study_number=1,
            zip_filename="x.zip",
            site_code="S001",
        )
        assert "sha256" not in item
        assert "size_bytes" not in item

    def test_nullable_numeric_fields_kept_as_null(self):
        sidecar = _effective_sidecar(rdsr=None)
        sidecar["series"] = []
        sidecar["patient"] = {"age_years": None, "weight_kg": None}
        item = rm.build_manifest_item(
            sidecar, study_number=1, zip_filename="x.zip", site_code="S001"
        )
        assert item["patient_weight_kg"] is None
        assert item["patient_age_years"] is None
        assert item["ctdivol_mgy"] is None
        assert item["dlp_mgy_cm"] is None


class TestManifestItemBlockers:
    def test_unset_image_quality_is_a_blocker(self):
        sidecar = _effective_sidecar(image_quality=None)
        item = rm.build_manifest_item(
            sidecar, study_number=1, zip_filename="x.zip", site_code="S001"
        )
        assert rm.manifest_item_blockers(item) == ["image_quality is not set"]

    def test_unresolved_indication_and_group_blockers(self):
        sidecar = _effective_sidecar()
        sidecar["indication"] = {
            "region": None,
            "clinical_indication": None,
            "iv_contrast": None,
        }
        sidecar["protocol_type"] = None
        sidecar["examination_group"] = None
        item = rm.build_manifest_item(
            sidecar, study_number=1, zip_filename="x.zip", site_code="S001"
        )
        blockers = rm.manifest_item_blockers(item)
        assert "clinical_indication_code is not set" in blockers
        assert "anatomical_region is not set" in blockers
        assert "contrast_code is not set" in blockers
        assert "patient_group_code is not set" in blockers

    def test_blank_string_counts_as_unset(self):
        assert rm.manifest_item_blockers({"site_code": "   "}) and \
            "site_code is not set" in rm.manifest_item_blockers({"site_code": "   "})


class TestBuildBatchManifest:
    def test_wraps_items_with_required_discriminators(self):
        item = rm.build_manifest_item(
            _effective_sidecar(),
            study_number=42,
            zip_filename="0042.zip",
            site_code="S001",
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
