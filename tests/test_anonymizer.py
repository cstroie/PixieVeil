"""Unit tests for Anonymizer: field-value strategies, UID consistency
mapping, and per-section anonymization behavior."""

import pydicom
import pytest
from pydicom.tag import Tag

from pixieveil.config import Settings
from pixieveil.processing.anonymizer import Anonymizer

_DEFAULT_PROFILE = dict(
    PatientName="PSEUDO", PatientID="PSEUDO", PatientBirthDate="CLEAR",
    PatientAge="KEEP", PatientSex="KEEP", InstitutionName="DEID_CENTER",
    StudyID="RESEARCH", StudyInstanceUID="PSEUDOUID", StudyDescription="KEEP",
    SeriesInstanceUID="PSEUDOUID", SeriesDescription="KEEP",
    FrameOfReferenceUID="PSEUDOUID", ReferringPhysicianName="CLEAR",
    OperatorsName="CLEAR", PerformingPhysicianName="CLEAR",
    AccessionNumber="CLEAR", KeepPrivateTags=False, PixelBlackout=False,
    RetainStudyDate=False,
)


def make_anonymizer(**profile_overrides) -> Anonymizer:
    profile = {**_DEFAULT_PROFILE, **profile_overrides}
    settings = Settings(anonymization={"profile": "test", "profiles": {"test": profile}})
    return Anonymizer(settings)


class TestApplyFieldValueStrategy:
    def test_none_strategy_clears(self):
        a = make_anonymizer()
        assert a.apply_field_value_strategy("Doe^John", None) is None

    def test_pseudo_strategy_is_deterministic(self):
        a = make_anonymizer()
        first = a.apply_field_value_strategy("Doe^John", "PSEUDO")
        second = a.apply_field_value_strategy("Doe^John", "PSEUDO")
        assert first == second

    def test_pseudo_strategy_case_insensitive(self):
        a = make_anonymizer()
        lower = a.apply_field_value_strategy("x", "pseudo")
        upper = a.apply_field_value_strategy("x", "PSEUDO")
        assert lower == upper

    def test_pseudouid_strategy_looks_like_a_uid(self):
        a = make_anonymizer()
        result = a.apply_field_value_strategy("1.2.3.4", "PSEUDOUID")
        assert result.startswith("2.25.")

    def test_newuid_strategy_ignores_original_value(self):
        # NEWUID means "generate a fresh one on every call" — not a
        # per-value-deterministic mapping like PSEUDOUID.
        a = make_anonymizer()
        first = a.apply_field_value_strategy("same-input", "NEWUID")
        second = a.apply_field_value_strategy("same-input", "NEWUID")
        assert first != second

    def test_keep_strategy_returns_original_unchanged(self):
        a = make_anonymizer()
        assert a.apply_field_value_strategy("034Y", "KEEP") == "034Y"

    def test_clear_strategy_returns_empty_string(self):
        a = make_anonymizer()
        assert a.apply_field_value_strategy("secret", "CLEAR") == ""

    def test_unknown_strategy_is_treated_as_a_literal_value(self):
        a = make_anonymizer()
        assert a.apply_field_value_strategy("secret", "DEID_CENTER") == "DEID_CENTER"


class TestGeneratePseudonym:
    def test_deterministic_for_same_input(self):
        a = make_anonymizer()
        assert a.generate_pseudonym("Doe^John") == a.generate_pseudonym("Doe^John")

    def test_different_inputs_produce_different_pseudonyms(self):
        a = make_anonymizer()
        assert a.generate_pseudonym("Doe^John") != a.generate_pseudonym("Smith^Jane")

    def test_format_is_eight_uppercase_hex_chars(self):
        a = make_anonymizer()
        pseudonym = a.generate_pseudonym("Doe^John")
        assert len(pseudonym) == 8
        assert pseudonym == pseudonym.upper()
        int(pseudonym, 16)  # raises ValueError if not valid hex


class TestGeneratePseudonymUid:
    def test_starts_with_25_root(self):
        a = make_anonymizer()
        assert a.generate_pseudonym_uid("1.2.3.4").startswith("2.25.")

    def test_deterministic_for_same_input(self):
        a = make_anonymizer()
        assert a.generate_pseudonym_uid("1.2.3.4") == a.generate_pseudonym_uid("1.2.3.4")


class TestSetField:
    def test_absent_field_is_a_noop(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        a.set_field(ds, "PatientName", "whatever")
        assert "PatientName" not in ds

    def test_none_value_clears_present_field(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientName = "Doe^John"
        a.set_field(ds, "PatientName", None)
        assert str(ds.PatientName) == ""

    def test_value_sets_present_field(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientName = "Doe^John"
        a.set_field(ds, "PatientName", "ANON-1234")
        assert str(ds.PatientName) == "ANON-1234"


class TestApplyUidMapping:
    def test_same_original_uid_maps_consistently(self):
        a = make_anonymizer()
        mapping = {}
        first = a.apply_uid_mapping("1.2.3", mapping, "PSEUDOUID")
        second = a.apply_uid_mapping("1.2.3", mapping, "PSEUDOUID")
        assert first == second
        assert mapping["1.2.3"] == first

    def test_different_original_uids_map_differently(self):
        a = make_anonymizer()
        mapping = {}
        first = a.apply_uid_mapping("1.2.3", mapping, "PSEUDOUID")
        second = a.apply_uid_mapping("4.5.6", mapping, "PSEUDOUID")
        assert first != second

    def test_newuid_strategy_is_still_cached_per_original_value(self):
        # apply_field_value_strategy("NEWUID") is non-deterministic on its
        # own, but apply_uid_mapping must cache the first result so every
        # image in the same series gets the SAME anonymized UID.
        a = make_anonymizer()
        mapping = {}
        first = a.apply_uid_mapping("1.2.3", mapping, "NEWUID")
        second = a.apply_uid_mapping("1.2.3", mapping, "NEWUID")
        assert first == second


class TestAnonymizePatientFields:
    def test_patient_name_gets_anon_prefixed_pseudonym(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientName = "Doe^John"
        a.anonymize_patient_fields(ds)
        assert str(ds.PatientName).startswith("ANON-")

    def test_patient_id_pseudo_has_no_anon_prefix(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientID = "MRN123"
        a.anonymize_patient_fields(ds)
        assert not str(ds.PatientID).startswith("ANON-")
        assert str(ds.PatientID) != "MRN123"

    def test_patient_birth_date_cleared(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientBirthDate = "19800101"
        a.anonymize_patient_fields(ds)
        assert str(ds.PatientBirthDate) == ""

    def test_patient_age_kept_never_cleared(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientAge = "034Y"
        a.anonymize_patient_fields(ds)
        assert str(ds.PatientAge) == "034Y"

    def test_patient_sex_kept(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientSex = "F"
        a.anonymize_patient_fields(ds)
        assert str(ds.PatientSex) == "F"

    def test_other_patient_ids_and_address_always_cleared(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.OtherPatientIDs = "MRN456"
        ds.PatientAddress = "123 Main St"
        a.anonymize_patient_fields(ds)
        assert ds.OtherPatientIDs == ""
        assert ds.PatientAddress == ""

    def test_absent_fields_are_skipped_without_error(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        a.anonymize_patient_fields(ds)  # must not raise
        assert "PatientName" not in ds


class TestAnonymizeStudySeriesFields:
    def test_study_and_series_uid_mapped_consistently_across_images(self):
        a = make_anonymizer()
        ds1 = pydicom.Dataset()
        ds1.StudyInstanceUID = "1.2.840.99999.1"
        ds1.SeriesInstanceUID = "1.2.840.99999.2"
        ds2 = pydicom.Dataset()
        ds2.StudyInstanceUID = "1.2.840.99999.1"
        ds2.SeriesInstanceUID = "1.2.840.99999.2"

        a.anonymize_study_series_fields(ds1)
        a.anonymize_study_series_fields(ds2)

        assert str(ds1.StudyInstanceUID) == str(ds2.StudyInstanceUID)
        assert str(ds1.SeriesInstanceUID) == str(ds2.SeriesInstanceUID)

    def test_sop_instance_uid_always_freshly_generated(self):
        a = make_anonymizer()
        ds1 = pydicom.Dataset()
        ds1.SOPInstanceUID = "1.2.840.99999.3"
        ds2 = pydicom.Dataset()
        ds2.SOPInstanceUID = "1.2.840.99999.3"

        a.anonymize_study_series_fields(ds1)
        a.anonymize_study_series_fields(ds2)

        # Same original SOPInstanceUID on both, but each image gets its
        # own new one — never mapped/cached like Study/SeriesInstanceUID.
        assert str(ds1.SOPInstanceUID) != str(ds2.SOPInstanceUID)

    def test_frame_of_reference_uid_deleted_when_strategy_clears_it(self):
        a = make_anonymizer(FrameOfReferenceUID=None)
        ds = pydicom.Dataset()
        ds.FrameOfReferenceUID = "1.2.840.99999.4"
        a.anonymize_study_series_fields(ds)
        assert "FrameOfReferenceUID" not in ds

    def test_study_id_literal_strategy(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.StudyID = "12345"
        a.anonymize_study_series_fields(ds)
        assert str(ds.StudyID) == "RESEARCH"

    def test_accession_number_cleared(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.AccessionNumber = "ACC001"
        a.anonymize_study_series_fields(ds)
        assert str(ds.AccessionNumber) == ""

    def test_study_and_series_description_kept(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.StudyDescription = "CT Head"
        ds.SeriesDescription = "Axial"
        a.anonymize_study_series_fields(ds)
        assert str(ds.StudyDescription) == "CT Head"
        assert str(ds.SeriesDescription) == "Axial"


class TestAnonymizeInstitutionPhysicianFields:
    def test_institution_name_literal_strategy(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.InstitutionName = "General Hospital"
        a.anonymize_institution_physician_fields(ds)
        assert str(ds.InstitutionName) == "DEID_CENTER"

    def test_physician_and_operator_names_cleared(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.ReferringPhysicianName = "Dr^Smith"
        ds.OperatorsName = "Tech^Jones"
        ds.PerformingPhysicianName = "Dr^Brown"
        a.anonymize_institution_physician_fields(ds)
        assert str(ds.ReferringPhysicianName) == ""
        assert str(ds.OperatorsName) == ""
        assert str(ds.PerformingPhysicianName) == ""

    def test_institution_address_always_cleared(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.InstitutionAddress = "123 Hospital Way"
        a.anonymize_institution_physician_fields(ds)
        assert ds.InstitutionAddress == ""


class TestAnonymizeDates:
    def test_retain_study_date_false_rewrites_study_and_acquisition_dates(self):
        a = make_anonymizer(RetainStudyDate=False)
        ds = pydicom.Dataset()
        ds.StudyDate = "20200101"
        ds.StudyTime = "120000"
        ds.AcquisitionDate = "20200101"
        ds.SeriesTime = "120500"
        a.anonymize_dates(ds)
        today = a.current_date()
        assert str(ds.StudyDate) == today
        assert str(ds.StudyTime) == a.current_time()
        assert str(ds.AcquisitionDate) == today
        assert str(ds.SeriesTime) == a.current_time()

    def test_retain_study_date_true_leaves_study_and_acquisition_dates(self):
        a = make_anonymizer(RetainStudyDate=True)
        ds = pydicom.Dataset()
        ds.StudyDate = "20200101"
        ds.StudyTime = "120000"
        ds.AcquisitionDate = "20200101"
        a.anonymize_dates(ds)
        assert str(ds.StudyDate) == "20200101"
        assert str(ds.StudyTime) == "120000"
        assert str(ds.AcquisitionDate) == "20200101"

    def test_instance_creation_and_content_dates_always_rewritten(self):
        # Unaffected by RetainStudyDate either way.
        a = make_anonymizer(RetainStudyDate=True)
        ds = pydicom.Dataset()
        ds.InstanceCreationDate = "19990101"
        ds.InstanceCreationTime = "010101"
        ds.ContentDate = "19990101"
        a.anonymize_dates(ds)
        today = a.current_date()
        assert str(ds.InstanceCreationDate) == today
        assert str(ds.InstanceCreationTime) == a.current_time()
        assert str(ds.ContentDate) == today


class TestRemoveSensitiveTags:
    def test_military_rank_removed_if_present(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.MilitaryRank = "Captain"
        a.remove_sensitive_tags(ds)
        assert "MilitaryRank" not in ds

    def test_absent_sensitive_tags_do_not_raise(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        a.remove_sensitive_tags(ds)  # must not raise


class TestHandlePrivateTags:
    def test_keep_private_tags_false_removes_them(self):
        a = make_anonymizer(KeepPrivateTags=False)
        ds = pydicom.Dataset()
        ds.add_new(Tag(0x0009, 0x0010), "LO", "PrivateValue")
        a.handle_private_tags(ds)
        assert Tag(0x0009, 0x0010) not in ds

    def test_keep_private_tags_true_preserves_them(self):
        a = make_anonymizer(KeepPrivateTags=True)
        ds = pydicom.Dataset()
        ds.add_new(Tag(0x0009, 0x0010), "LO", "PrivateValue")
        a.handle_private_tags(ds)
        assert Tag(0x0009, 0x0010) in ds


class TestRemoveOverlays:
    def test_overlay_group_tags_removed(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.add_new(Tag(0x6000, 0x3000), "OW", b"\x00\x00")
        a.remove_overlays(ds)
        assert Tag(0x6000, 0x3000) not in ds

    def test_non_overlay_tags_untouched(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientName = "Doe^John"
        a.remove_overlays(ds)
        assert "PatientName" in ds


class TestHandlePixelBlackout:
    def _pixel_dataset(self):
        np = pytest.importorskip("numpy")
        ds = pydicom.Dataset()
        ds.file_meta = pydicom.dataset.FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
        ds.Rows = 2
        ds.Columns = 2
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.PixelData = np.array([[1, 2], [3, 4]], dtype=np.uint16).tobytes()
        return ds

    def test_pixel_blackout_true_zeros_pixel_data(self):
        a = make_anonymizer(PixelBlackout=True)
        ds = self._pixel_dataset()
        a.handle_pixel_blackout(ds)
        assert ds.pixel_array.sum() == 0

    def test_pixel_blackout_false_leaves_pixel_data(self):
        a = make_anonymizer(PixelBlackout=False)
        ds = self._pixel_dataset()
        a.handle_pixel_blackout(ds)
        assert ds.pixel_array.sum() != 0

    def test_pixel_blackout_failure_raises_instead_of_shipping_unredacted_data(self):
        # Regression: this used to catch its own exception and just log a
        # warning, so a study configured with PixelBlackout=True could ship
        # with un-redacted pixel data while the pipeline believed
        # anonymization succeeded. It must now propagate, so
        # StorageManager.process_image's existing anonymization-error
        # handling drops the image instead.
        a = make_anonymizer(PixelBlackout=True)
        ds = pydicom.Dataset()
        ds.PixelData = b"\x00\x00"  # no Rows/Columns/etc -> pixel_array raises
        with pytest.raises(Exception):
            a.handle_pixel_blackout(ds)


class TestAnonymizeIntegration:
    def test_burned_in_annotation_forced_to_no(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.BurnedInAnnotation = "YES"
        a.anonymize(ds)
        assert str(ds.BurnedInAnnotation) == "NO"

    def test_returns_the_same_dataset_instance(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        assert a.anonymize(ds) is ds

    def test_uid_lookup_helpers_reflect_anonymized_values(self):
        a = make_anonymizer()
        ds = pydicom.Dataset()
        ds.PatientID = "MRN123"
        ds.StudyInstanceUID = "1.2.840.99999.1"
        ds.SeriesInstanceUID = "1.2.840.99999.2"
        a.anonymize(ds)

        assert a.get_patient_id_mapping("MRN123") == str(ds.PatientID)
        assert a.get_study_uid_mapping("1.2.840.99999.1") == str(ds.StudyInstanceUID)
        assert a.get_series_uid_mapping("1.2.840.99999.2") == str(ds.SeriesInstanceUID)

    def test_unmapped_uid_lookup_returns_none(self):
        a = make_anonymizer()
        assert a.get_study_uid_mapping("never-seen") is None
