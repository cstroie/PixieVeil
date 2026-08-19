"""Unit tests for SeriesFilter: modality exclusion, original-vs-derived
series detection, and attribute-based include/exclude rules."""

import pydicom

from pixieveil.config import Settings
from pixieveil.processing.series_filter import SeriesFilter


def make_filter(**series_filter_cfg) -> SeriesFilter:
    return SeriesFilter(Settings(series_filter=series_filter_cfg))


class TestCompileRules:
    def test_valid_pattern_compiled(self):
        rules = SeriesFilter.compile_rules({"SeriesDescription": "topogram"})
        assert len(rules) == 1
        assert rules[0][0] == "SeriesDescription"

    def test_invalid_pattern_is_skipped_not_raised(self):
        rules = SeriesFilter.compile_rules({"SeriesDescription": "["})  # unbalanced regex
        assert rules == []

    def test_empty_rules_dict(self):
        assert SeriesFilter.compile_rules({}) == []


class TestIsOriginalSeries:
    def test_missing_image_type_is_original(self):
        sf = make_filter()
        ds = pydicom.Dataset()
        assert sf.is_original_series(ds) is True

    def test_multivalue_original_primary_axial(self):
        sf = make_filter()
        ds = pydicom.Dataset()
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        assert sf.is_original_series(ds) is True

    def test_multivalue_derived_is_not_original(self):
        sf = make_filter()
        ds = pydicom.Dataset()
        ds.ImageType = ["DERIVED", "SECONDARY"]
        assert sf.is_original_series(ds) is False

    def test_scalar_string_original_is_original(self):
        # Regression: pydicom hands back a plain str, not a MultiValue,
        # when the wire only carried one ImageType value. Indexing a str
        # with [0] takes its first character ("O"), which used to
        # misclassify this as derived and silently drop the image.
        sf = make_filter()
        ds = pydicom.Dataset()
        ds.ImageType = "ORIGINAL"
        assert sf.is_original_series(ds) is True

    def test_scalar_string_derived_is_not_original(self):
        sf = make_filter()
        ds = pydicom.Dataset()
        ds.ImageType = "DERIVED"
        assert sf.is_original_series(ds) is False

    def test_case_insensitive(self):
        sf = make_filter()
        ds = pydicom.Dataset()
        ds.ImageType = ["original", "primary"]
        assert sf.is_original_series(ds) is True

    def test_empty_multivalue_is_not_original(self):
        sf = make_filter()
        ds = pydicom.Dataset()
        ds.ImageType = []
        assert sf.is_original_series(ds) is False


class TestShouldFilterModality:
    def test_excluded_modality_is_filtered(self):
        sf = make_filter(exclude_modalities=["SR"], only_original_series=False)
        ds = pydicom.Dataset()
        ds.Modality = "SR"
        assert sf.should_filter(ds) is True

    def test_non_excluded_modality_is_kept(self):
        sf = make_filter(exclude_modalities=["SR"], only_original_series=False)
        ds = pydicom.Dataset()
        ds.Modality = "CT"
        assert sf.should_filter(ds) is False

    def test_no_modality_tag_is_kept(self):
        sf = make_filter(exclude_modalities=["SR"], only_original_series=False)
        ds = pydicom.Dataset()
        assert sf.should_filter(ds) is False


class TestShouldFilterOriginalSeries:
    def test_default_only_original_series_filters_derived(self):
        sf = make_filter()  # only_original_series defaults to True
        ds = pydicom.Dataset()
        ds.ImageType = ["DERIVED", "SECONDARY"]
        assert sf.should_filter(ds) is True

    def test_default_only_original_series_keeps_original(self):
        sf = make_filter()
        ds = pydicom.Dataset()
        ds.ImageType = ["ORIGINAL", "PRIMARY"]
        assert sf.should_filter(ds) is False

    def test_only_original_series_false_keeps_derived(self):
        sf = make_filter(only_original_series=False)
        ds = pydicom.Dataset()
        ds.ImageType = ["DERIVED", "SECONDARY"]
        assert sf.should_filter(ds) is False


class TestShouldFilterAttributeRules:
    def test_exclude_rule_match_filters_out(self):
        sf = make_filter(
            only_original_series=False,
            exclude={"SeriesDescription": "(?i)topogram"},
        )
        ds = pydicom.Dataset()
        ds.SeriesDescription = "Topogram 1.0"
        assert sf.should_filter(ds) is True

    def test_exclude_rule_no_match_is_kept(self):
        sf = make_filter(
            only_original_series=False,
            exclude={"SeriesDescription": "(?i)topogram"},
        )
        ds = pydicom.Dataset()
        ds.SeriesDescription = "Axial 5mm"
        assert sf.should_filter(ds) is False

    def test_include_rule_overrides_exclude_rule(self):
        sf = make_filter(
            only_original_series=False,
            include={"SeriesDescription": "(?i)dose"},
            exclude={"Modality": "SC"},
        )
        ds = pydicom.Dataset()
        ds.Modality = "SC"
        ds.SeriesDescription = "Dose Report"
        assert sf.should_filter(ds) is False  # include wins

    def test_include_rule_no_match_falls_through_to_exclude(self):
        sf = make_filter(
            only_original_series=False,
            include={"SeriesDescription": "(?i)dose"},
            exclude={"Modality": "SC"},
        )
        ds = pydicom.Dataset()
        ds.Modality = "SC"
        ds.SeriesDescription = "Screenshot"
        assert sf.should_filter(ds) is True

    def test_missing_attribute_does_not_match_rule(self):
        sf = make_filter(
            only_original_series=False,
            exclude={"SeriesDescription": "(?i)topogram"},
        )
        ds = pydicom.Dataset()  # no SeriesDescription at all
        assert sf.should_filter(ds) is False

    def test_multivalue_attribute_matched_value_by_value(self):
        sf = make_filter(
            only_original_series=False,
            exclude={"ImageType": "LOCALIZER"},
        )
        ds = pydicom.Dataset()
        ds.ImageType = ["ORIGINAL", "PRIMARY", "LOCALIZER"]
        assert sf.should_filter(ds) is True
