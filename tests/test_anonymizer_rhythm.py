"""The `rhythm` anonymization profile shipped in config/settings.example.yaml
must produce DICOM that passes RHYTHM's GDPR-strict ingest validator.

Two checks:
  * drift — every "must be removed" tag in RHYTHM's own vendored
    GDPR-strict.json that pydicom can address by keyword is handled by the
    profile (RemoveTags, or a clearing field strategy). Fails loudly if a
    `git submodule update --remote reference/uploader` adds one.
  * round-trip — a synthetic CT carrying the exact tags a real upload was
    rejected for (reference/anon_rhythm.json) comes out clean.
"""

import json
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.datadict import tag_for_keyword
from pydicom.tag import Tag
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from pixieveil.config import Settings
from pixieveil.processing.anonymizer import Anonymizer

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_SETTINGS = _REPO_ROOT / "config" / "settings.example.yaml"
_GDPR_STRICT = (
    _REPO_ROOT / "reference" / "uploader" / "ct_upload_platform" / "GDPR-strict.json"
)

# top-level profile flags in GDPR-strict.json, not per-tag rules
_PROFILE_FLAGS = {"KeepPrivateTags", "PixelBlackout", "RetainStudyDate", "Private attributes"}

# tags the validator rejects when the report was captured (all 49 files)
_REJECTED_IN_REAL_UPLOAD = [
    "StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate",
    "StudyTime", "SeriesTime", "AcquisitionTime", "ContentTime",
    "InstitutionName", "StationName", "StudyDescription", "SeriesDescription",
    "PatientName", "PatientID", "DeviceSerialNumber", "StudyID",
    "Manufacturer", "ManufacturerModelName", "ProtocolName",
]


def _rhythm_profile():
    if not _EXAMPLE_SETTINGS.exists():
        pytest.skip("config/settings.example.yaml not found")
    settings = Settings.load(_EXAMPLE_SETTINGS)
    return settings.get_anonymization_profile("rhythm")


def _rhythm_anonymizer():
    settings = Settings.load(_EXAMPLE_SETTINGS)
    settings.anonymization["profile"] = "rhythm"
    return Anonymizer(settings)


def _synthetic_ct():
    """A CT slice carrying every tag a real RHYTHM upload was rejected for,
    plus a private tag, an overlay group and a curve group."""
    ds = pydicom.Dataset()
    ds.preamble = b"\x00" * 128
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5"
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = "1.2.3.4.5"
    ds.StudyInstanceUID = "1.2.840.111.1"
    ds.SeriesInstanceUID = "1.2.840.111.2"
    ds.Modality = "CT"

    ds.StudyDate = "20260819"
    ds.SeriesDate = "20260819"
    ds.AcquisitionDate = "20260819"
    ds.ContentDate = "20260820"
    ds.StudyTime = "214611"
    ds.SeriesTime = "220302"
    ds.AcquisitionTime = "215528"
    ds.ContentTime = "220302"
    ds.InstitutionName = "DEID_CENTER"
    ds.StationName = "CTAWP79098"
    ds.StudyDescription = "Head^CAP_SPIRAL_NATIV (Adult)"
    ds.SeriesDescription = "CEREBRAL  5.0  H31s"
    ds.PatientName = "ANON-662DAC1E"
    ds.PatientID = "C74D7EAF"
    ds.DeviceSerialNumber = "79098"
    ds.StudyID = "RESEARCH"
    ds.Manufacturer = "SIEMENS"
    ds.ManufacturerModelName = "Emotion 16"
    ds.ProtocolName = "CAP_SPIRAL_NATIV"

    # KEEP tags RHYTHM needs — must survive
    ds.PatientAge = "006Y"
    ds.PatientWeight = "20.0"

    # non-tag-rule checks the validator also runs
    ds.add_new(Tag(0x0009, 0x0010), "LO", "SIEMENS PRIVATE")
    ds.add_new(Tag(0x6000, 0x3000), "OW", b"\x00\x00")     # overlay data
    ds.add_new(Tag(0x5000, 0x3000), "OW", b"\x00\x00")     # curve data
    return ds


class TestDriftAgainstVendoredSchema:
    def test_every_removable_keyword_is_handled_by_the_profile(self):
        if not _GDPR_STRICT.exists():
            pytest.skip("reference/uploader submodule not checked out")
        rules = json.loads(_GDPR_STRICT.read_text())
        profile = _rhythm_profile()
        remove = set(profile.RemoveTags)
        # A named field set to a clearing strategy (CLEAR, or null/None in the
        # YAML -> empty string at anonymize time) also satisfies "must be
        # removed" for that keyword. Exclude the non-tag config fields.
        _NON_TAG = {"RemoveTags", "KeepPrivateTags", "PixelBlackout", "RetainStudyDate"}
        cleared_by_strategy = {
            name for name, val in profile.model_dump().items()
            if name not in _NON_TAG
            and (val is None or (isinstance(val, str) and val.upper() == "CLEAR"))
        }

        unhandled = []
        for keyword, action in rules.items():
            if action is not None or keyword in _PROFILE_FLAGS:
                continue
            if tag_for_keyword(keyword) is None:
                continue  # display-name form; the validator can't address it either
            if keyword in remove or keyword in cleared_by_strategy:
                continue
            unhandled.append(keyword)

        assert not unhandled, (
            "GDPR-strict.json marks these 'must be removed' and the rhythm "
            f"profile does not handle them: {sorted(unhandled)}"
        )


class TestRoundTrip:
    def test_rejected_tags_come_out_absent_or_empty(self):
        anon = _rhythm_anonymizer()
        ds = anon.anonymize(_synthetic_ct())
        for kw in _REJECTED_IN_REAL_UPLOAD:
            present = kw in ds
            value = str(getattr(ds, kw, "")).strip() if present else ""
            assert value == "", f"{kw} still carries a value after rhythm anonymization: {value!r}"

    def test_study_and_series_uid_regenerated_present(self):
        ds = _synthetic_ct()
        orig_study, orig_series = ds.StudyInstanceUID, ds.SeriesInstanceUID
        out = _rhythm_anonymizer().anonymize(ds)
        assert out.StudyInstanceUID and out.StudyInstanceUID != orig_study
        assert out.SeriesInstanceUID and out.SeriesInstanceUID != orig_series

    def test_keep_tags_rhythm_needs_survive(self):
        out = _rhythm_anonymizer().anonymize(_synthetic_ct())
        assert str(out.PatientAge) == "006Y"
        assert str(out.PatientWeight) == "20.0"

    def test_private_overlay_and_curve_groups_stripped(self):
        out = _rhythm_anonymizer().anonymize(_synthetic_ct())
        assert Tag(0x0009, 0x0010) not in out       # KeepPrivateTags: false
        assert Tag(0x6000, 0x3000) not in out       # overlay
        assert Tag(0x5000, 0x3000) not in out       # curve

    def test_patient_id_is_gone_not_pseudonymised(self):
        out = _rhythm_anonymizer().anonymize(_synthetic_ct())
        assert "PatientID" not in out or str(out.PatientID).strip() == ""

    def test_rejected_tags_absent_after_save_and_reload(self, tmp_path):
        """RHYTHM validates the written file, not our in-memory Dataset."""
        ds = _rhythm_anonymizer().anonymize(_synthetic_ct())
        path = tmp_path / "0001.dcm"
        ds.save_as(path, enforce_file_format=False)
        reloaded = pydicom.dcmread(path)
        for kw in _REJECTED_IN_REAL_UPLOAD:
            present = kw in reloaded
            value = str(getattr(reloaded, kw, "")).strip() if present else ""
            assert value == "", f"{kw} survives save/reload: {value!r}"

    def test_file_meta_sop_uid_synced_not_leaking_original(self, tmp_path):
        ds = _synthetic_ct()
        original = str(ds.file_meta.MediaStorageSOPInstanceUID)
        out = _rhythm_anonymizer().anonymize(ds)
        path = tmp_path / "0001.dcm"
        out.save_as(path, enforce_file_format=False)
        reloaded = pydicom.dcmread(path)
        assert reloaded.file_meta.MediaStorageSOPInstanceUID != original
        assert reloaded.file_meta.MediaStorageSOPInstanceUID == reloaded.SOPInstanceUID


class TestStripTagsUnit:
    def test_strip_tags_deletes_listed_present_tags_only(self):
        from tests.test_anonymizer import make_anonymizer
        a = make_anonymizer(RemoveTags=["StationName", "ProtocolName", "NotPresentTag"])
        ds = pydicom.Dataset()
        ds.StationName = "CTAWP1"
        ds.Modality = "CT"
        a.strip_tags(ds)
        assert "StationName" not in ds
        assert "Modality" in ds        # untouched
