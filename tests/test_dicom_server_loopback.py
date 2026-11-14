"""C-STORE into PixieVeil's own handlers via a real pynetdicom association.

test_dicom_server.py feeds handle_c_store a hand-built FakeEvent; here the
same DicomServer.handle_c_store / handle_echo are registered on a real AE
bound to 127.0.0.1 (not DicomServer.start(), which binds all interfaces),
and a real SCU sends to it — so event.dataset / event.file_meta are what
pynetdicom really produces, and the image goes all the way through the real
StorageManager.
"""

import socket

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, ImplicitVRLittleEndian, generate_uid
from pynetdicom import AE, evt
from pynetdicom.sop_class import Verification

from pixieveil.config import Settings
from pixieveil.dicom_server.server import SUPPORTED_STORAGE_SOP_CLASSES, DicomServer
from pixieveil.storage.storage_manager import StorageManager

from .conftest import make_settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pixieveil_scp(tmp_path):
    sm = StorageManager(make_settings(tmp_path))
    server = DicomServer(Settings(), sm)
    ae = AE(ae_title="PIXIEVEIL")
    ae.add_supported_context(Verification)
    for sop in SUPPORTED_STORAGE_SOP_CLASSES:
        ae.add_supported_context(sop)
    port = _free_port()
    scp = ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[
        (evt.EVT_C_ECHO, server.handle_echo), (evt.EVT_C_STORE, server.handle_c_store)])
    try:
        yield port, sm
    finally:
        scp.shutdown()


def _ct(study_uid, series_uid, n, ts=ExplicitVRLittleEndian):
    ds = Dataset()
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = generate_uid()
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study_uid, series_uid
    ds.Modality = "CT"
    ds.PatientID, ds.PatientName = "REAL-ID-123", "Doe^Jane"
    ds.PatientBirthDate = "19800101"
    ds.InstanceNumber = n
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation, ds.SamplesPerPixel = 15, 0, 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\0\1" * 4
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ts
    return ds


def _send(port, datasets, ts=ExplicitVRLittleEndian):
    ae = AE(ae_title="MODALITY")
    ae.add_requested_context(Verification)
    ae.add_requested_context(CTImageStorage, ts)
    assoc = ae.associate("127.0.0.1", port, ae_title="PIXIEVEIL")
    assert assoc.is_established
    try:
        assert assoc.send_c_echo().Status == 0x0000
        return [assoc.send_c_store(ds).Status for ds in datasets]
    finally:
        assoc.release()


class TestLoopbackReceive:
    @pytest.mark.parametrize("ts", [ExplicitVRLittleEndian, ImplicitVRLittleEndian])
    def test_images_land_anonymized_in_the_study_tree(self, pixieveil_scp, ts):
        port, sm = pixieveil_scp
        study, series = generate_uid(), generate_uid()
        statuses = _send(port, [_ct(study, series, i, ts) for i in (1, 2)], ts)

        assert statuses == [0x0000, 0x0000]
        files = sorted(sm.base_path.glob("0001/0001/*.dcm"))
        assert [f.name for f in files] == ["0001.dcm", "0002.dcm"]
        for f in files:
            ds = pydicom.dcmread(f)
            assert ds.StudyInstanceUID != study            # UIDs remapped
            assert str(ds.get("PatientID", "")) != "REAL-ID-123"
            assert "Doe" not in str(ds.get("PatientName", ""))
            assert ds.PixelData == b"\0\1" * 4             # pixels intact
        assert not list(sm.temp_path.glob("*.dcm"))        # nothing raw left

    def test_dataset_missing_required_uid_is_refused(self, pixieveil_scp):
        port, sm = pixieveil_scp
        ds = _ct(generate_uid(), generate_uid(), 1)
        del ds.SeriesInstanceUID
        (status,) = _send(port, [ds])
        assert status != 0x0000
        assert not list(sm.base_path.rglob("*.dcm"))
        assert not list(sm.temp_path.glob("*.dcm"))
