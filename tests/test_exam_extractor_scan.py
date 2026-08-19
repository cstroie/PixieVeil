"""Fixture-based tests for ExamExtractor._scan_dicom_files — the highest-
complexity, previously-untested function in the codebase (radon: E/40).
It has no test-suite dependency on self, but is exercised through a real
ExamExtractor instance with lookup_dir pointed at an empty tmp_path so it
never touches the real integrations/rhythm/*.yaml files.

Where a field is genuinely "first value seen wins" (manufacturer, protocol
name, per-series technique fields), each fixture puts that value on exactly
one file so the assertion doesn't depend on pydicom.Path.rglob() iteration
order, which the OS does not guarantee. Where the real logic is
order-independent (CTDIvol max-tracking, z-extent min/max), fixtures
legitimately use multiple conflicting values.
"""

import pydicom

from pixieveil.processing.exam_extractor import ExamExtractor

from .conftest import write_minimal_dicom


def make_extractor(tmp_path) -> ExamExtractor:
    # Empty lookup_dir: keeps this test hermetic against the real
    # integrations/rhythm/*.yaml files, which _scan_dicom_files never
    # touches anyway (that's ExamExtractor._match_indication's job).
    return ExamExtractor(lookup_dir=tmp_path / "empty_lookup")


class TestEmptyStudy:
    def test_no_dcm_files(self, tmp_path):
        study_dir = tmp_path / "study"
        study_dir.mkdir()
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert scanned["dcm_files"] == []
        assert scanned["manufacturer"] is None
        assert scanned["model"] is None
        assert scanned["protocol_name"] == ""
        assert scanned["age_years"] is None
        assert scanned["weight_kg"] is None
        assert scanned["rdsr_data"] is None
        assert scanned["series_records"] == {}


class TestStudyLevelHeaders:
    def test_manufacturer_model_protocol_captured(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            Manufacturer="ACME", ManufacturerModelName="Scanner 3000",
            ProtocolName="Head Routine", StudyDescription="CT Head",
            BodyPartExamined="HEAD", SeriesNumber=1,
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert scanned["manufacturer"] == "ACME"
        assert scanned["model"] == "Scanner 3000"
        assert scanned["protocol_name"] == "Head Routine"
        assert scanned["study_description"] == "CT Head"
        assert scanned["body_part"] == "HEAD"

    def test_contrast_bolus_agent_present_sets_contrast_seen(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            ContrastBolusAgent="Omnipaque", SeriesNumber=1,
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["contrast_seen"] is True

    def test_no_contrast_bolus_agent_leaves_contrast_seen_false(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", SeriesNumber=1)
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["contrast_seen"] is False

    def test_blank_contrast_bolus_agent_does_not_set_contrast_seen(self, tmp_path):
        # A present-but-blank tag (scanner writes an empty string rather
        # than omitting the element) must not be treated as "had contrast".
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             ContrastBolusAgent="   ", SeriesNumber=1)
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["contrast_seen"] is False

    def test_age_and_weight_parsed(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            PatientAge="034Y", PatientWeight=72.5, SeriesNumber=1,
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["age_years"] == 34.0
        assert scanned["weight_kg"] == 72.5

    def test_corrupt_dcm_file_is_skipped_without_crashing(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "good.dcm",
            Manufacturer="ACME", SeriesNumber=1,
        )
        (study_dir / "0001" / "bad.dcm").parent.mkdir(parents=True, exist_ok=True)
        (study_dir / "0001" / "bad.dcm").write_bytes(b"not a dicom file")

        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert scanned["manufacturer"] == "ACME"
        assert len(scanned["dcm_files"]) == 2  # both counted, only one parsed

    def test_pre_deface_backup_dir_excluded_from_scan(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Manufacturer="ACME", SeriesNumber=1)
        write_minimal_dicom(study_dir / "0001_pre_deface" / "img1.dcm",
                             Manufacturer="OTHER", SeriesNumber=1)

        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert len(scanned["dcm_files"]) == 1
        assert scanned["manufacturer"] == "ACME"


class TestSeriesRecords:
    def test_non_ct_image_contributes_headers_but_no_series_record(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "dose_report.dcm",
            Modality="SC", Manufacturer="ACME", SeriesNumber=1,
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert scanned["manufacturer"] == "ACME"
        assert scanned["series_records"] == {}

    def test_ct_image_creates_series_record_keyed_by_series_number(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            Modality="CT", SeriesNumber=7, SeriesDescription="Axial 5mm",
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert set(scanned["series_records"].keys()) == {"7"}
        rec = scanned["series_records"]["7"]
        assert rec["series_number"] == 7
        assert rec["series_description"] == "Axial 5mm"
        assert rec["is_topogram"] is False

    def test_missing_series_number_uses_question_mark_key(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT")
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert set(scanned["series_records"].keys()) == {"?"}
        assert scanned["series_records"]["?"]["series_number"] is None

    def test_localizer_image_type_flags_topogram(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            Modality="CT", SeriesNumber=1,
            ImageType=["ORIGINAL", "PRIMARY", "LOCALIZER"],
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["series_records"]["1"]["is_topogram"] is True

    def test_two_series_produce_two_records(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1, SeriesDescription="Topogram")
        write_minimal_dicom(study_dir / "0002" / "img1.dcm",
                             Modality="CT", SeriesNumber=2, SeriesDescription="Axial")
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert set(scanned["series_records"].keys()) == {"1", "2"}

    def test_ctdivol_tracks_maximum_within_a_series(self, tmp_path):
        # Order-independent by construction: whichever file the OS hands
        # back first, the recorded value must end up as the max of the two.
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1, CTDIvol=5.0)
        write_minimal_dicom(study_dir / "0001" / "img2.dcm",
                             Modality="CT", SeriesNumber=1, CTDIvol=8.0)
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["series_records"]["1"]["ctdi_vol_mgy"] == 8.0

    def test_z_position_tracks_min_and_max_within_a_series(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1,
                             ImagePositionPatient=[0.0, 0.0, 30.0])
        write_minimal_dicom(study_dir / "0001" / "img2.dcm",
                             Modality="CT", SeriesNumber=1,
                             ImagePositionPatient=[0.0, 0.0, -10.0])
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        rec = scanned["series_records"]["1"]
        assert rec["z_min_mm"] == -10.0
        assert rec["z_max_mm"] == 30.0

    def test_technique_fields_captured_and_type_converted(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            Modality="CT", SeriesNumber=1,
            SliceThickness=5.0, ExposureTime=800,
            PatientPosition="HFS",
            SingleCollimationWidth=0.6, TotalCollimationWidth=38.4,
            SpiralPitchFactor=0.9,
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        rec = scanned["series_records"]["1"]
        assert rec["slice_thickness_mm"] == 5.0
        assert rec["exposure_time_ms"] == 800.0
        assert rec["patient_position"] == "HFS"
        assert rec["single_collimation_width_mm"] == 0.6
        assert rec["total_collimation_width_mm"] == 38.4
        assert rec["spiral_pitch_factor"] == 0.9

    def test_multivalued_convolution_kernel_joined_with_slash(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            Modality="CT", SeriesNumber=1, ConvolutionKernel=["B", "70f"],
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["series_records"]["1"]["convolution_kernel"] == "B/70f"


class TestRdsrDetection:
    def _rdsr_root_dataset(self, **extra_attrs):
        concept = pydicom.Dataset()
        concept.CodeValue = "113701"  # X-Ray Radiation Dose Report
        attrs = {
            "ConceptNameCodeSequence": pydicom.Sequence([concept]),
            "ContentSequence": pydicom.Sequence([]),
        }
        attrs.update(extra_attrs)
        return attrs

    def test_rdsr_content_detected_and_populated(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "rdsr.dcm",
            Modality="SR",  # excluded from series_records, still scanned for RDSR
            **self._rdsr_root_dataset(),
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert scanned["rdsr_data"] == {"total_dlp_mgy_cm": None, "acquisitions": []}
        assert scanned["series_records"] == {}  # SR modality, not CT

    def test_no_rdsr_content_leaves_it_none(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1)
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["rdsr_data"] is None
