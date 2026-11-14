"""DicomStorage against a *real* pynetdicom Storage SCP on 127.0.0.1.

test_dicom_storage.py drives the send loop through a fake association that
accepts every dataset. A real association only carries the presentation
contexts the remote node accepted, and pynetdicom's send_c_store raises
(rather than returning a status) for a dataset with no accepted context —
e.g. a PACS that takes CT but refuses the RDSR / Dose Report screen
capture. Loopback only: nothing leaves the machine.
"""

import socket

import pytest
from pydicom.uid import CTImageStorage, SecondaryCaptureImageStorage
from pynetdicom import AE, evt
from pynetdicom.sop_class import XRayRadiationDoseSRStorage

from pixieveil.config import Settings
from pixieveil.storage.dicom_storage import DicomStorage

from .conftest import write_minimal_dicom


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def scp():
    """A real Storage SCP accepting only CT Image Storage."""
    received: list[str] = []

    def on_store(event):
        received.append(str(event.dataset.SOPInstanceUID))
        return 0x0000

    ae = AE(ae_title="TESTSCP")
    ae.add_supported_context(CTImageStorage)
    port = _free_port()
    server = ae.start_server(("127.0.0.1", port), block=False,
                             evt_handlers=[(evt.EVT_C_STORE, on_store)])
    try:
        yield port, received
    finally:
        server.shutdown()


def _storage(port) -> DicomStorage:
    return DicomStorage(Settings(storage={"remote_storage": {"dicom": {
        "host": "127.0.0.1", "port": port, "ae_title": "TESTSCP"}}}))


def _ct(path):
    write_minimal_dicom(path, SOPClassUID=CTImageStorage, Modality="CT")


class TestLoopbackSend:
    def test_all_ct_study_is_sent(self, tmp_path, scp):
        port, received = scp
        for i in range(3):
            _ct(tmp_path / "0001" / f"{i}.dcm")
        assert _storage(port)._send_study_sync(tmp_path) is True
        assert len(received) == 3

    @pytest.mark.parametrize("refused_sop", [SecondaryCaptureImageStorage,
                                             XRayRadiationDoseSRStorage])
    def test_refused_sop_class_fails_that_file_but_sends_the_rest(
            self, tmp_path, scp, refused_sop):
        # The refused object sorts *first*, so an exception that escapes the
        # loop would leave every CT image unsent.
        port, received = scp
        write_minimal_dicom(tmp_path / "0000" / "dose.dcm",
                            SOPClassUID=refused_sop, Modality="SR")
        for i in range(2):
            _ct(tmp_path / "0001" / f"{i}.dcm")

        ok = _storage(port)._send_study_sync(tmp_path)

        assert ok is False                 # not everything arrived
        assert len(received) == 2          # but every CT image did

    def test_unreachable_node_returns_false(self, tmp_path):
        _ct(tmp_path / "0001" / "a.dcm")
        assert _storage(_free_port())._send_study_sync(tmp_path) is False
