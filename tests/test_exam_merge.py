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
