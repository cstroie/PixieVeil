"""Unit tests for the manual-edit overlay semantics in exam_merge.py."""

from pixieveil.processing import exam_merge


class TestIsEditablePath:
    def test_known_simple_path(self):
        assert exam_merge.is_editable_path("patient.weight_kg") is True

    def test_unknown_path(self):
        assert exam_merge.is_editable_path("patient.name") is False

    def test_series_family_path(self):
        assert exam_merge.is_editable_path("series.3.dlp_mgy_cm") is True

    def test_series_path_bad_index(self):
        assert exam_merge.is_editable_path("series.abc.dlp_mgy_cm") is False

    def test_series_path_unknown_leaf(self):
        # Same family shape, but "patient_name" isn't one of the whitelisted
        # per-series leaves.
        assert exam_merge.is_editable_path("series.3.patient_name") is False


class TestApplyManual:
    def test_simple_path_written(self):
        data = {"patient": {"weight_kg": None}}
        manual = {"edited_at": "t", "fields": {"patient.weight_kg": 34.0}}
        out = exam_merge.apply_manual(data, manual)
        assert out["patient"]["weight_kg"] == 34.0
        assert out["manual"] == manual

    def test_series_field_written_to_matching_series(self):
        data = {"series": [{"series_number": 2, "dlp_mgy_cm": None}]}
        manual = {"edited_at": "t", "fields": {"series.2.dlp_mgy_cm": 55.0}}
        out = exam_merge.apply_manual(data, manual)
        assert out["series"][0]["dlp_mgy_cm"] == 55.0

    def test_series_field_with_no_matching_series_is_dropped_with_note(self):
        data = {"series": [{"series_number": 1, "dlp_mgy_cm": None}], "notes": []}
        manual = {"edited_at": "t", "fields": {"series.99.dlp_mgy_cm": 55.0}}
        out = exam_merge.apply_manual(data, manual)
        assert out["series"][0]["dlp_mgy_cm"] is None
        assert any("series 99" in n for n in out["notes"])

    def test_unknown_path_is_ignored_not_written(self):
        data = {}
        manual = {"edited_at": "t", "fields": {"totally.unknown.path": "x"}}
        out = exam_merge.apply_manual(data, manual)
        assert "totally" not in out

    def test_manual_block_always_recorded_even_if_fields_empty(self):
        data = {}
        manual = {"edited_at": "t", "fields": {}}
        out = exam_merge.apply_manual(data, manual)
        assert out["manual"] == manual


class TestMergeExtracted:
    def test_previous_manual_overlay_survives_reextraction(self):
        fresh = {
            "patient": {"weight_kg": None, "age_years": None},
            "indication": {"region": None, "clinical_indication": None},
        }
        previous = {
            "manual": {
                "edited_at": "t",
                "fields": {"patient.weight_kg": 12.0, "indication.region": "Head"},
            }
        }
        merged = exam_merge.merge_extracted(fresh, previous)
        assert merged["patient"]["weight_kg"] == 12.0
        assert merged["indication"]["region"] == "Head"

    def test_no_previous_manual_data_is_a_noop(self):
        fresh = {"patient": {"weight_kg": None}}
        merged = exam_merge.merge_extracted(fresh, {})
        assert merged["patient"]["weight_kg"] is None
        assert merged["manual"] == {"edited_at": None, "fields": {}}


class TestRecomputeBuckets:
    def test_manually_supplied_weight_updates_bucket(self):
        data = {
            "indication": {"region": "Abdomen", "clinical_indication": "Acute abdomen"},
            "patient": {"age_years": None, "weight_kg": 12.0},
            "notes": ["PatientWeight absent on all images — needs manual entry"],
        }
        exam_merge.recompute_buckets(data)
        assert data["protocol_type"] == "PEDIATRIC_BODY"
        assert data["examination_group"] is not None
        # The stale "needs manual entry" note should have been dropped once
        # a real value resolved the bucket.
        assert not any("PatientWeight absent" in n for n in data["notes"])


class TestMissingRequired:
    def _full(self) -> dict:
        return {
            "image_quality": "Diagnostic",
            "patient": {"age_years": 8.0, "weight_kg": 25.0},
            "indication": {"region": "Chest", "clinical_indication": "Fungal infections",
                           "iv_contrast": "Non-contrast"},
            "scanner": {"rhythm_scanner_id": "uuid"},
            "rhythm": {"protocol_key": "head_trauma"},
        }

    def test_complete_exam_has_nothing_missing(self):
        assert exam_merge.missing_required(self._full()) == []

    def test_none_means_everything_missing(self):
        assert exam_merge.missing_required(None) == list(exam_merge.REQUIRED_PATHS)

    def test_null_blank_and_absent_all_count_as_missing(self):
        data = self._full()
        data["image_quality"] = None
        data["rhythm"]["protocol_key"] = "   "
        del data["patient"]["weight_kg"]
        assert exam_merge.missing_required(data) == [
            "image_quality", "patient.weight_kg", "rhythm.protocol_key",
        ]

    def test_zero_is_a_value_not_missing(self):
        data = self._full()
        data["patient"]["age_years"] = 0
        assert exam_merge.missing_required(data) == []

    def test_weight_optional_for_head_and_mastoid(self):
        for region in ("Head", "Mastoid bone/Inner Ear"):
            data = self._full()
            data["indication"]["region"] = region
            data["patient"]["weight_kg"] = None
            assert exam_merge.missing_required(data) == []
            assert "patient.weight_kg" not in exam_merge.required_paths(data)

    def test_weight_optional_for_pediatric_head_protocol(self):
        data = self._full()
        data["indication"]["region"] = "Chest"
        data["protocol_type"] = "PEDIATRIC_HEAD"
        data["patient"]["weight_kg"] = None
        assert exam_merge.missing_required(data) == []

    def test_weight_required_for_body_regions(self):
        data = self._full()
        data["indication"]["region"] = "Chest"
        data["patient"]["weight_kg"] = None
        assert exam_merge.missing_required(data) == ["patient.weight_kg"]
        assert len(exam_merge.required_paths(data)) == len(exam_merge.REQUIRED_PATHS)

    def test_non_dict_intermediate_is_missing_not_an_error(self):
        data = self._full()
        data["patient"] = None
        assert exam_merge.missing_required(data) == ["patient.weight_kg", "patient.age_years"]
