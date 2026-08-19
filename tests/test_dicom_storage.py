"""Unit tests for DicomStorage: config resolution and the C-STORE send loop.

_send_study_sync talks to a real network association via pynetdicom's AE,
which we never want a unit test to actually open. Instead of a mocking
framework, we substitute pixieveil.storage.dicom_storage.AE (via pytest's
built-in monkeypatch fixture) with a small fake AE/association pair that
records what it was asked to do and returns a scripted C-STORE status per
file — the same "fake object, not a mock library" style used for the DICOM
fixtures elsewhere in this suite.
"""

import asyncio

import pytest

import pixieveil.storage.dicom_storage as dicom_storage_module
from pixieveil.config import Settings
from pixieveil.storage.dicom_storage import DicomStorage

from .conftest import read_dicom, write_minimal_dicom


class FakeStatus:
    def __init__(self, code: int):
        self.Status = code


class FakeAssociation:
    def __init__(self, is_established: bool = True, status_by_sop: dict | None = None,
                 default_status: int = 0x0000):
        self.is_established = is_established
        self._status_by_sop = status_by_sop or {}
        self._default_status = default_status
        self.sent_sop_uids: list[str] = []
        self.released = False

    def send_c_store(self, ds):
        self.sent_sop_uids.append(str(ds.SOPInstanceUID))
        code = self._status_by_sop.get(str(ds.SOPInstanceUID), self._default_status)
        if code is None:
            return None  # simulates "no response"
        return FakeStatus(code)

    def release(self):
        self.released = True


class FakeAE:
    """Records associate() calls; returns a pre-built FakeAssociation."""

    last_instance = None

    def __init__(self, ae_title=None):
        self.ae_title = ae_title
        self.requested_contexts = None
        self.associate_calls: list[tuple] = []
        self.association_to_return = FakeAssociation()
        FakeAE.last_instance = self

    def associate(self, host, port, ae_title=None):
        self.associate_calls.append((host, port, ae_title))
        return self.association_to_return


def install_fake_ae(monkeypatch, association: FakeAssociation) -> None:
    """Patch dicom_storage.AE so the next _send_study_sync() call gets
    `association` back from associate()."""
    FakeAE.last_instance = None

    class ScriptedAE(FakeAE):
        def __init__(self, ae_title=None):
            super().__init__(ae_title)
            self.association_to_return = association

    monkeypatch.setattr(dicom_storage_module, "AE", ScriptedAE)


@pytest.fixture(autouse=True)
def fake_ae(monkeypatch):
    """Default: every test gets a fresh, successful FakeAssociation unless
    it calls install_fake_ae() itself with different scripted behavior."""
    install_fake_ae(monkeypatch, FakeAssociation())
    yield


def make_storage(**dicom_cfg) -> DicomStorage:
    cfg = {"host": "127.0.0.1", "port": 4070, "ae_title": "REMOTE"}
    cfg.update(dicom_cfg)
    settings = Settings(storage={"remote_storage": {"dicom": cfg}})
    return DicomStorage(settings)


class TestConfig:
    def test_enabled_when_host_and_port_set(self):
        assert make_storage().enabled is True

    def test_disabled_when_host_missing(self):
        settings = Settings(storage={"remote_storage": {"dicom": {"port": 4070}}})
        assert DicomStorage(settings).enabled is False

    def test_disabled_when_port_missing(self):
        settings = Settings(storage={"remote_storage": {"dicom": {"host": "1.2.3.4"}}})
        assert DicomStorage(settings).enabled is False

    def test_disabled_when_not_configured_at_all(self):
        assert DicomStorage(Settings()).enabled is False

    def test_ae_title_defaults_to_any_scp(self):
        settings = Settings(storage={"remote_storage": {"dicom": {"host": "h", "port": 1}}})
        assert DicomStorage(settings).ae_title == "ANY-SCP"

    def test_calling_ae_falls_back_to_dicom_server_ae_title(self):
        settings = Settings(
            storage={"remote_storage": {"dicom": {"host": "h", "port": 1}}},
            dicom_server={"ae_title": "PIXIEVEIL_SCP"},
        )
        assert DicomStorage(settings).calling_ae == "PIXIEVEIL_SCP"

    def test_calling_ae_explicit_override_wins(self):
        settings = Settings(
            storage={"remote_storage": {
                "dicom": {"host": "h", "port": 1, "calling_ae": "CUSTOM"}
            }},
            dicom_server={"ae_title": "PIXIEVEIL_SCP"},
        )
        assert DicomStorage(settings).calling_ae == "CUSTOM"


class TestSendStudyNotEnabled:
    def test_returns_false_without_touching_ae(self, tmp_path):
        storage = DicomStorage(Settings())  # no remote_storage.dicom configured
        result = asyncio.run(storage.send_study(tmp_path))
        assert result is False
        assert FakeAE.last_instance is None


class TestSendStudySync:
    def test_no_dcm_files_returns_false_without_associating(self, tmp_path):
        storage = make_storage()
        result = storage._send_study_sync(tmp_path)
        assert result is False
        assert FakeAE.last_instance is None

    def test_association_not_established_returns_false(self, tmp_path, monkeypatch):
        write_minimal_dicom(tmp_path / "0001" / "img1.dcm")
        install_fake_ae(monkeypatch, FakeAssociation(is_established=False))
        storage = make_storage()

        result = storage._send_study_sync(tmp_path)

        assert result is False

    def test_successful_send_of_all_files(self, tmp_path):
        write_minimal_dicom(tmp_path / "0001" / "img1.dcm")
        write_minimal_dicom(tmp_path / "0001" / "img2.dcm")
        storage = make_storage()

        result = storage._send_study_sync(tmp_path)

        assert result is True
        assoc = FakeAE.last_instance.association_to_return
        assert len(assoc.sent_sop_uids) == 2
        assert assoc.released is True

    def test_one_failed_file_makes_the_whole_send_fail(self, tmp_path, monkeypatch):
        p1 = tmp_path / "0001" / "img1.dcm"
        p2 = tmp_path / "0001" / "img2.dcm"
        write_minimal_dicom(p1)
        write_minimal_dicom(p2)
        failing_sop = str(read_dicom(p1).SOPInstanceUID)
        association = FakeAssociation(status_by_sop={failing_sop: 0xA700})
        install_fake_ae(monkeypatch, association)
        storage = make_storage()

        result = storage._send_study_sync(tmp_path)

        assert result is False
        # Both files still get attempted, and the association is released,
        # even though one of them failed.
        assert len(association.sent_sop_uids) == 2
        assert association.released is True

    def test_no_response_status_counts_as_failure(self, tmp_path, monkeypatch):
        p1 = tmp_path / "0001" / "img1.dcm"
        write_minimal_dicom(p1)
        sop = str(read_dicom(p1).SOPInstanceUID)
        install_fake_ae(monkeypatch, FakeAssociation(status_by_sop={sop: None}))
        storage = make_storage()

        result = storage._send_study_sync(tmp_path)

        assert result is False

    def test_unreadable_file_counts_as_error_but_others_still_sent(self, tmp_path):
        write_minimal_dicom(tmp_path / "0001" / "good.dcm")
        (tmp_path / "0001" / "bad.dcm").write_bytes(b"not a dicom file")
        storage = make_storage()

        result = storage._send_study_sync(tmp_path)

        assert result is False  # one file was unreadable -> overall failure
        assoc = FakeAE.last_instance.association_to_return
        assert len(assoc.sent_sop_uids) == 1  # only the good file was sent
        assert assoc.released is True

    def test_pre_deface_backup_dir_excluded(self, tmp_path):
        write_minimal_dicom(tmp_path / "0001" / "img1.dcm")
        write_minimal_dicom(tmp_path / "0001_pre_deface" / "img1.dcm")
        storage = make_storage()

        result = storage._send_study_sync(tmp_path)

        assert result is True
        assert len(FakeAE.last_instance.association_to_return.sent_sop_uids) == 1


class TestSendStudyDelegation:
    def test_send_study_offloads_to_send_study_sync(self, tmp_path, monkeypatch):
        storage = make_storage()
        calls = []

        def fake_sync(study_dir):
            calls.append(study_dir)
            return True

        monkeypatch.setattr(storage, "_send_study_sync", fake_sync)
        result = asyncio.run(storage.send_study(tmp_path))

        assert result is True
        assert calls == [tmp_path]
