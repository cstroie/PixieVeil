"""Shared fixtures for the StorageManager / StudyManager test suite.

Everything here is offline: DICOM export (dicom_storage/remote_storage) and
defacing stay disabled unless a test explicitly configures them, so no test
makes a network call, needs a GPU, or needs nnU-Net installed.
"""

from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from pixieveil.config import Settings
from pixieveil.storage.storage_manager import StorageManager


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """A minimal Settings instance rooted under tmp_path. dicom/http export
    and defacing are left unconfigured (disabled) unless overridden."""
    base = {
        "storage": {
            "base_path": str(tmp_path / "data"),
            "temp_path": str(tmp_path / "tmp"),
        },
        "logging": {"anontrail": str(tmp_path / "anontrail.jsonl")},
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def storage_manager(tmp_path) -> StorageManager:
    return StorageManager(make_settings(tmp_path))


def write_minimal_dicom(path: Path, **attrs) -> None:
    """Write a tiny-but-valid .dcm file so path-touching code (dcmread/
    save_as, rglob("*.dcm")) has something real to operate on."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    # A real DICOM file always carries SOPInstanceUID at the dataset level
    # too (normally identical to file_meta's MediaStorageSOPInstanceUID) —
    # code that reads ds.SOPInstanceUID directly (e.g. DicomStorage's
    # C-STORE loop) needs it present, not just the file_meta copy.
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.Modality = attrs.pop("Modality", "CT")
    for key, value in attrs.items():
        setattr(ds, key, value)

    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path), enforce_file_format=True, little_endian=True, implicit_vr=False)


def read_dicom(path: Path) -> "pydicom.Dataset":
    return pydicom.dcmread(str(path))
