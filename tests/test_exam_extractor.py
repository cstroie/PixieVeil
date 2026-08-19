"""Unit tests for the pure/deterministic helpers in exam_extractor.py.

These deliberately avoid touching ExamExtractor.extract() itself (needs a
study_dir full of .dcm files) or the RDSR content-tree parser (needs a full
SR dataset fixture) — this covers the standalone functions extract() and
exam_merge.recompute_buckets() both build on.
"""

import pydicom

from pixieveil.processing.exam_extractor import (
    _determine_bucket,
    _estimate_series_dlp,
    _first_float,
    _first_str,
    _is_ct_image,
    _is_topogram,
    _parse_dicom_age,
    HEAD_INDICATIONS,
)


class TestParseDicomAge:
    def test_years(self):
        assert _parse_dicom_age("034Y") == 34.0

    def test_months(self):
        assert _parse_dicom_age("006M") == 0.5

    def test_weeks(self):
        assert _parse_dicom_age("010W") is not None

    def test_days(self):
        assert round(_parse_dicom_age("365D"), 2) == 1.0

    def test_none_input(self):
        assert _parse_dicom_age(None) is None

    def test_empty_string(self):
        assert _parse_dicom_age("") is None

    def test_too_short(self):
        assert _parse_dicom_age("1Y") is None

    def test_non_digit_prefix(self):
        assert _parse_dicom_age("0XYZ") is None

    def test_unknown_unit(self):
        assert _parse_dicom_age("012Z") is None

    def test_newborn(self):
        assert _parse_dicom_age("000Y") == 0.0


class TestEstimateSeriesDlp:
    def _rec(self, **overrides):
        rec = {
            "is_topogram": False,
            "ctdi_vol_mgy": 10.0,
            "z_min_mm": 0.0,
            "z_max_mm": 100.0,
            "slice_thickness_mm": 5.0,
        }
        rec.update(overrides)
        return rec

    def test_topogram_never_estimated(self):
        assert _estimate_series_dlp(self._rec(is_topogram=True)) is None

    def test_no_ctdi_returns_none(self):
        assert _estimate_series_dlp(self._rec(ctdi_vol_mgy=None)) is None

    def test_normal_case(self):
        # length = (100 - 0 + 5) mm = 105mm = 10.5cm; DLP = 10 * 10.5 = 105
        assert _estimate_series_dlp(self._rec()) == 105.0

    def test_single_image_series_uses_slice_thickness(self):
        rec = self._rec(z_min_mm=None, z_max_mm=None, slice_thickness_mm=2.0)
        # length = 2mm = 0.2cm; DLP = 10 * 0.2 = 2.0
        assert _estimate_series_dlp(rec) == 2.0

    def test_no_position_and_no_thickness_returns_none(self):
        rec = self._rec(z_min_mm=None, z_max_mm=None, slice_thickness_mm=None)
        assert _estimate_series_dlp(rec) is None

    def test_z_max_not_greater_than_z_min_falls_back_to_thickness(self):
        # A single-slice or corrupted-order series: z_max == z_min doesn't
        # satisfy z_max > z_min, so this should fall back to slice thickness
        # rather than producing a zero/negative length estimate.
        rec = self._rec(z_min_mm=50.0, z_max_mm=50.0, slice_thickness_mm=3.0)
        assert _estimate_series_dlp(rec) == 3.0  # 10 * 0.3cm


class TestDetermineBucket:
    def test_head_trauma_is_a_head_indication(self):
        # Sanity-check the fixture used below actually exercises the
        # head-tab branch, not the weight-bucketed body branch.
        assert ("Head", "Trauma") in HEAD_INDICATIONS

    def test_unresolved_indication_returns_nulls(self):
        protocol_type, group, notes = _determine_bucket(None, None, 5.0, 20.0)
        assert protocol_type is None
        assert group is None
        assert notes

    def test_heavy_weight_forces_young_adult(self):
        region, indication = "Head", "Trauma"
        protocol_type, group, notes = _determine_bucket(region, indication, 10.0, 85.0)
        assert protocol_type == "YOUNG_ADULT"
        assert any("weight > 80" in n for n in notes)

    def test_adult_age_forces_young_adult(self):
        region, indication = "Head", "Trauma"
        protocol_type, group, notes = _determine_bucket(region, indication, 18.0, 40.0)
        assert protocol_type == "YOUNG_ADULT"

    def test_head_indication_buckets_by_age(self):
        region, indication = "Head", "Trauma"
        protocol_type, group, notes = _determine_bucket(region, indication, 3.0, None)
        assert protocol_type == "PEDIATRIC_HEAD"
        assert group == "Group 3 – Early Childhood"

    def test_head_indication_missing_age_leaves_group_null(self):
        region, indication = "Head", "Trauma"
        protocol_type, group, notes = _determine_bucket(region, indication, None, None)
        assert protocol_type == "PEDIATRIC_HEAD"
        assert group is None
        assert any("age unknown" in n for n in notes)

    def test_non_head_indication_buckets_by_weight(self):
        protocol_type, group, notes = _determine_bucket("Abdomen", "Acute abdomen", 8.0, 12.0)
        assert protocol_type == "PEDIATRIC_BODY"
        assert group == "Group 2 – Infant, Toddler and Early Childhood"

    def test_non_head_indication_missing_weight_leaves_group_null(self):
        protocol_type, group, notes = _determine_bucket("Abdomen", "Acute abdomen", 8.0, None)
        assert protocol_type == "PEDIATRIC_BODY"
        assert group is None
        assert any("weight unknown" in n for n in notes)


class TestIsCtImage:
    def test_ct_modality(self):
        ds = pydicom.Dataset()
        ds.Modality = "CT"
        assert _is_ct_image(ds) is True

    def test_missing_modality_treated_as_ct(self):
        ds = pydicom.Dataset()
        assert _is_ct_image(ds) is True

    def test_other_modality_excluded(self):
        ds = pydicom.Dataset()
        ds.Modality = "SC"
        assert _is_ct_image(ds) is False


class TestFirstFloat:
    def test_sets_value_when_unset(self):
        rec = {"x": None}
        _first_float(rec, "x", "5.5")
        assert rec["x"] == 5.5

    def test_does_not_overwrite_existing_value(self):
        # This is the "first value wins" guarantee _scan_dicom_files relies
        # on: a series' technique params are constant per-series, so later
        # images in the same series must not clobber the first reading.
        rec = {"x": 1.0}
        _first_float(rec, "x", 99.0)
        assert rec["x"] == 1.0

    def test_none_raw_leaves_unset(self):
        rec = {"x": None}
        _first_float(rec, "x", None)
        assert rec["x"] is None

    def test_empty_string_raw_leaves_unset(self):
        rec = {"x": None}
        _first_float(rec, "x", "")
        assert rec["x"] is None

    def test_unparseable_raw_leaves_unset_without_raising(self):
        # Guards _scan_dicom_files against a malformed/non-conformant value
        # a real scanner or a force=True read could hand back.
        rec = {"x": None}
        _first_float(rec, "x", "not-a-number")
        assert rec["x"] is None

    def test_missing_key_defaults_to_none(self):
        rec = {}
        _first_float(rec, "x", "3.0")
        assert rec["x"] == 3.0


class TestFirstStr:
    def test_sets_value_when_unset(self):
        rec = {"x": None}
        _first_str(rec, "x", "HFS")
        assert rec["x"] == "HFS"

    def test_does_not_overwrite_existing_value(self):
        rec = {"x": "HFS"}
        _first_str(rec, "x", "FFS")
        assert rec["x"] == "HFS"

    def test_multivalue_joined_with_slash(self):
        rec = {"x": None}
        _first_str(rec, "x", ["B", "70f"])
        assert rec["x"] == "B/70f"

    def test_none_raw_leaves_unset(self):
        rec = {"x": None}
        _first_str(rec, "x", None)
        assert rec["x"] is None

    def test_non_string_scalar_is_stringified(self):
        rec = {"x": None}
        _first_str(rec, "x", 42)
        assert rec["x"] == "42"


class TestIsTopogram:
    def test_localizer_image_type(self):
        ds = pydicom.Dataset()
        ds.ImageType = ["ORIGINAL", "PRIMARY", "LOCALIZER"]
        assert _is_topogram(ds) is True

    def test_normal_image_type(self):
        ds = pydicom.Dataset()
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        assert _is_topogram(ds) is False

    def test_missing_image_type(self):
        ds = pydicom.Dataset()
        assert _is_topogram(ds) is False
