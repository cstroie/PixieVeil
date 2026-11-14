"""Fixture-based tests for ExamExtractor._scan_dicom_files — the highest-
complexity, previously-untested function in the codebase (radon: E/40).
It has no test-suite dependency on self, but is exercised through a real
ExamExtractor instance with no protocol catalog (nothing to match against).

Where a field is genuinely "first value seen wins" (manufacturer, protocol
name, per-series technique fields), each fixture puts that value on exactly
one file so the assertion doesn't depend on pydicom.Path.rglob() iteration
order, which the OS does not guarantee. Where the real logic is
order-independent (CTDIvol max-tracking, z-extent min/max), fixtures
legitimately use multiple conflicting values.
"""

import pydicom

from pixieveil.processing.exam_extractor import (
    ExamExtractor,
    _parse_rdsr_content,
    _sr_text_or_code_value,
)

from .conftest import write_minimal_dicom


def make_extractor(tmp_path) -> ExamExtractor:
    # No catalog: _scan_dicom_files never consults one anyway.
    return ExamExtractor()


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

    def test_hints_fill_blank_headers(self, tmp_path):
        """rhythm-anonymized study: Manufacturer/model/descriptions gone from
        the files, supplied via the pre-anon hints instead."""
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT",
                            SeriesNumber=3)  # no Manufacturer/ProtocolName/etc.
        hints = {
            "study": {"manufacturer": "SIEMENS", "model": "Emotion 16",
                      "protocol_name": "CAP_SPIRAL_NATIV",
                      "study_description": "Head^CAP_SPIRAL_NATIV"},
            # keyed by local folder (0001 → "1"), not DICOM SeriesNumber 3
            "series": {"1": {"series_description": "CEREBRAL 5.0 H31s"}},
        }
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir, hints)

        assert scanned["manufacturer"] == "SIEMENS"
        assert scanned["model"] == "Emotion 16"
        assert scanned["protocol_name"] == "CAP_SPIRAL_NATIV"
        assert scanned["study_description"] == "Head^CAP_SPIRAL_NATIV"
        assert scanned["series_records"]["0001"]["series_description"] == "CEREBRAL 5.0 H31s"

    def test_series_hints_follow_folder_not_series_number(self, tmp_path):
        """Scanner sent series out of order (seen on a SOMATOM go.All): the
        recon (SeriesNumber 2) arrived first → folder 0001, the topogram
        (SeriesNumber 1) later → folder 0002. Hints are keyed by folder."""
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT", SeriesNumber=2)
        write_minimal_dicom(study_dir / "0002" / "img1.dcm", Modality="CT", SeriesNumber=1,
                            ImageType=["ORIGINAL", "PRIMARY", "LOCALIZER"])
        hints = {"series": {"1": {"series_description": "Torace Nativ 1,50 Br40"},
                            "2": {"series_description": "Topogram 0,70 Tr20 cor"}}}
        recs = make_extractor(tmp_path)._scan_dicom_files(study_dir, hints)["series_records"]

        assert recs["0002"]["series_description"] == "Topogram 0,70 Tr20 cor"
        assert recs["0001"]["series_description"] == "Torace Nativ 1,50 Br40"
        assert all("_folder" not in r for r in recs.values())

    def test_real_header_wins_over_hint(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT",
                            SeriesNumber=1, Manufacturer="ACME",
                            SeriesDescription="from header")
        hints = {"study": {"manufacturer": "SIEMENS"},
                 "series": {"1": {"series_description": "from hint"}}}
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir, hints)
        assert scanned["manufacturer"] == "ACME"
        assert scanned["series_records"]["0001"]["series_description"] == "from header"

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

    def test_ct_image_creates_series_record_keyed_by_folder(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            Modality="CT", SeriesNumber=7, SeriesDescription="Axial 5mm",
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)

        assert set(scanned["series_records"].keys()) == {"0001"}
        rec = scanned["series_records"]["0001"]
        assert rec["series_number"] == 7
        assert rec["series_description"] == "Axial 5mm"
        assert rec["is_topogram"] is False

    def test_missing_series_number_kept_as_none(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT")
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert set(scanned["series_records"].keys()) == {"0001"}
        assert scanned["series_records"]["0001"]["series_number"] is None

    def test_localizer_image_type_flags_topogram(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(
            study_dir / "0001" / "img1.dcm",
            Modality="CT", SeriesNumber=1,
            ImageType=["ORIGINAL", "PRIMARY", "LOCALIZER"],
        )
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["series_records"]["0001"]["is_topogram"] is True

    def test_two_series_produce_two_records(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1, SeriesDescription="Topogram")
        write_minimal_dicom(study_dir / "0002" / "img1.dcm",
                             Modality="CT", SeriesNumber=2, SeriesDescription="Axial")
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert set(scanned["series_records"].keys()) == {"0001", "0002"}

    def test_two_series_sharing_a_series_number_stay_separate(self, tmp_path):
        """Distinct series (own folder / SeriesInstanceUID) that reuse one
        SeriesNumber — e.g. a localizer and the axial run — must not merge:
        a merge marks the axial run a topogram (no DLP) and unions z-extents."""
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1, SeriesDescription="Topogram",
                             ImageType=["ORIGINAL", "PRIMARY", "LOCALIZER"],
                             ImagePositionPatient=[0.0, 0.0, 400.0])
        for i, z in enumerate((0.0, 100.0)):
            write_minimal_dicom(study_dir / "0002" / f"img{i}.dcm",
                                 Modality="CT", SeriesNumber=1, SeriesDescription="Axial",
                                 CTDIvol=10.0, SliceThickness=5.0,
                                 ImagePositionPatient=[0.0, 0.0, z])
        ex = make_extractor(tmp_path)
        scanned = ex._scan_dicom_files(study_dir)
        recs = sorted(scanned["series_records"].values(),
                      key=lambda r: r["series_description"])
        assert [r["series_description"] for r in recs] == ["Axial", "Topogram"]
        axial, topo = recs
        assert axial["is_topogram"] is False and topo["is_topogram"] is True
        assert (axial["z_min_mm"], axial["z_max_mm"]) == (0.0, 100.0)

        series, _ = ex._build_series_list(scanned["series_records"])
        assert [s["series_number"] for s in series] == [1, 1]
        by_desc = {s["series_description"]: s for s in series}
        assert by_desc["Axial"]["dlp_mgy_cm"] == 105.0   # 10 × (100 + 5) mm
        assert by_desc["Topogram"]["dlp_mgy_cm"] is None

    def test_series_without_series_number_stay_separate(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesDescription="A")
        write_minimal_dicom(study_dir / "0002" / "img1.dcm",
                             Modality="CT", SeriesDescription="B")
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert sorted(r["series_description"]
                      for r in scanned["series_records"].values()) == ["A", "B"]

    def test_ctdivol_tracks_maximum_within_a_series(self, tmp_path):
        # Order-independent by construction: whichever file the OS hands
        # back first, the recorded value must end up as the max of the two.
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1, CTDIvol=5.0)
        write_minimal_dicom(study_dir / "0001" / "img2.dcm",
                             Modality="CT", SeriesNumber=1, CTDIvol=8.0)
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        assert scanned["series_records"]["0001"]["ctdi_vol_mgy"] == 8.0

    def test_z_position_tracks_min_and_max_within_a_series(self, tmp_path):
        study_dir = tmp_path / "study"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm",
                             Modality="CT", SeriesNumber=1,
                             ImagePositionPatient=[0.0, 0.0, 30.0])
        write_minimal_dicom(study_dir / "0001" / "img2.dcm",
                             Modality="CT", SeriesNumber=1,
                             ImagePositionPatient=[0.0, 0.0, -10.0])
        scanned = make_extractor(tmp_path)._scan_dicom_files(study_dir)
        rec = scanned["series_records"]["0001"]
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
        rec = scanned["series_records"]["0001"]
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
        assert scanned["series_records"]["0001"]["convolution_kernel"] == "B/70f"


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


def _code(value, meaning=None, scheme="DCM"):
    c = pydicom.Dataset()
    c.CodeValue, c.CodingSchemeDesignator = value, scheme
    if meaning is not None:
        c.CodeMeaning = meaning
    return c


def _sr_item(code, value_type, children=None, **attrs):
    item = pydicom.Dataset()
    item.ValueType = value_type
    item.ConceptNameCodeSequence = pydicom.Sequence([_code(code)])
    for k, v in attrs.items():
        setattr(item, k, v)
    if children is not None:
        item.ContentSequence = pydicom.Sequence(children)
    return item


def _text(code, text):
    return _sr_item(code, "TEXT", TextValue=text)


def _coded(code, meaning):
    return _sr_item(code, "CODE", ConceptCodeSequence=pydicom.Sequence([_code("X", meaning)]))


def _num(code, value):
    mv = pydicom.Dataset()
    mv.NumericValue = value
    return _sr_item(code, "NUM", MeasuredValueSequence=pydicom.Sequence([mv]))


class TestSrTextOrCodeValue:
    """_sr_text_or_code_value: the reader behind every textual RDSR field
    (acquisition protocol/type, target region, modulation type)."""

    def test_text_item_returns_text_value(self):
        assert _sr_text_or_code_value(_text("125203", "Head 120kV")) == "Head 120kV"

    def test_code_item_returns_code_meaning(self):
        assert _sr_text_or_code_value(_coded("113820", "Spiral Acquisition")) == \
            "Spiral Acquisition"

    def test_code_item_uses_first_concept_code(self):
        item = _sr_item("113820", "CODE", ConceptCodeSequence=pydicom.Sequence(
            [_code("P5-08001", "Spiral Acquisition"), _code("X", "Other")]))
        assert _sr_text_or_code_value(item) == "Spiral Acquisition"

    def test_blank_text_is_none(self):
        assert _sr_text_or_code_value(_text("125203", "")) is None

    def test_missing_text_value_is_none(self):
        assert _sr_text_or_code_value(_sr_item("125203", "TEXT")) is None

    def test_code_without_concept_code_sequence_is_none(self):
        assert _sr_text_or_code_value(_sr_item("113820", "CODE")) is None

    def test_code_with_empty_concept_code_sequence_is_none(self):
        item = _sr_item("113820", "CODE", ConceptCodeSequence=pydicom.Sequence([]))
        assert _sr_text_or_code_value(item) is None

    def test_code_without_code_meaning_is_none(self):
        item = _sr_item("113820", "CODE",
                        ConceptCodeSequence=pydicom.Sequence([_code("P5-08001")]))
        assert _sr_text_or_code_value(item) is None

    def test_other_value_types_are_none(self):
        # A NUM / CONTAINER / missing ValueType never yields text, even if a
        # stray TextValue is present.
        assert _sr_text_or_code_value(_num("113733", "120")) is None
        assert _sr_text_or_code_value(_sr_item("113819", "CONTAINER", [])) is None
        stray = pydicom.Dataset()
        stray.TextValue = "x"
        assert _sr_text_or_code_value(stray) is None


class TestRdsrAcquisitionParsing:
    """End to end through _parse_rdsr_content: textual fields come out of
    CODE and TEXT items, nested at any depth inside a CT Acquisition."""

    def _report(self, *acquisitions, total_dlp="512.3"):
        ds = pydicom.Dataset()
        ds.ConceptNameCodeSequence = pydicom.Sequence([_code("113701")])
        accumulated = _sr_item("113811", "CONTAINER", [_num("113813", total_dlp)])
        ds.ContentSequence = pydicom.Sequence([accumulated, *acquisitions])
        return ds

    def _acquisition(self, **over):
        params = _sr_item("113822", "CONTAINER", [      # CT Acquisition Parameters
            _num("113828", "0.8"),                       # pitch
            _sr_item("113831", "CONTAINER", [            # CT X-Ray Source Parameters
                _num("113733", "120"), _num("113833", "300"),
                _num("113734", "180"), _num("113834", "0.5"),
            ]),
        ])
        dose = _sr_item("113829", "CONTAINER", [         # CT Dose
            _num("113830", "45.2"), _num("113838", "812.5"),
        ])
        children = [
            over.get("protocol", _text("125203", "Head Routine")),
            over.get("region", _coded("123014", "Head")),
            over.get("acq_type", _coded("113820", "Spiral Acquisition")),
            over.get("context", _coded("G-C32C", "CT without contrast")),
            params, dose,
            over.get("modulation", _text("113842", "XYZ_EC")),
        ]
        return _sr_item("113819", "CONTAINER", [c for c in children if c is not None])

    def test_text_and_code_fields_parsed(self):
        rdsr = _parse_rdsr_content(self._report(self._acquisition()))
        assert rdsr["total_dlp_mgy_cm"] == 512.3
        (acq,) = rdsr["acquisitions"]
        assert acq == {
            "acquisition_protocol": "Head Routine",
            "target_region": "Head",
            "acquisition_type": "Spiral Acquisition",
            "procedure_context": "CT without contrast",
            "pitch": 0.8, "kvp": 120.0, "mean_ma": 180.0, "max_ma": 300.0,
            "rotation_time_s": 0.5, "mean_ctdivol_mgy": 45.2, "dlp_mgy_cm": 812.5,
            "modulation_type": "XYZ_EC",
        }

    def test_vendor_variants_text_for_code_and_code_for_text(self):
        # Vendors differ: acquisition type as TEXT, protocol as CODE.
        acq = self._acquisition(protocol=_coded("125203", "Thorax Std"),
                                acq_type=_text("113820", "Sequenced Acquisition"))
        (parsed,) = _parse_rdsr_content(self._report(acq))["acquisitions"]
        assert parsed["acquisition_protocol"] == "Thorax Std"
        assert parsed["acquisition_type"] == "Sequenced Acquisition"

    def test_missing_or_blank_text_fields_are_none(self):
        acq = self._acquisition(protocol=None, region=_text("123014", ""),
                                modulation=_sr_item("113842", "CODE"))
        (parsed,) = _parse_rdsr_content(self._report(acq))["acquisitions"]
        assert parsed["acquisition_protocol"] is None
        assert parsed["target_region"] is None
        assert parsed["modulation_type"] is None
        assert parsed["kvp"] == 120.0                  # rest still parsed

    def test_each_acquisition_keeps_its_own_values(self):
        topo = self._acquisition(protocol=_text("125203", "Topogram"),
                                 acq_type=_coded("113820", "Constant Angle Acquisition"))
        spiral = self._acquisition()
        acqs = _parse_rdsr_content(self._report(topo, spiral))["acquisitions"]
        assert [(a["acquisition_protocol"], a["acquisition_type"]) for a in acqs] == [
            ("Topogram", "Constant Angle Acquisition"),
            ("Head Routine", "Spiral Acquisition"),
        ]
