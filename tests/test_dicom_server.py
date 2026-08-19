"""Unit tests for the DICOM server: CStoreSCPHandler (validation + the
save/process delegation) and DicomServer (config, the C-ECHO/C-STORE event
adapters, the port-bind guard in start(), and stop()'s bookkeeping).

CStoreSCPHandler.handle_c_store's own responsibility — validate, then
delegate to StorageManager.save_temp_image/process_image, map exceptions to
DICOM status codes — is tested against a small FakeStorageManager (records
calls, can be told to raise) rather than the real StorageManager, so these
tests fail only when the handler's own logic breaks, not when unrelated
StorageManager internals change (those have their own test file). One
integration test at the bottom wires up the real StorageManager fixture to
confirm the two objects' actual method signatures still match.

DicomServer.start_blocking_server/the live pynetdicom association loop are
out of scope — actually opening a DICOM association needs a real socket
server thread and a real C-STORE client, which belongs in an end-to-end
test, not a unit test.
"""

import asyncio
import socket

import pydicom
import pytest

from pixieveil.config import Settings
from pixieveil.dicom_server.handlers import CStoreSCPHandler
from pixieveil.dicom_server.server import DicomServer

from .conftest import make_settings, read_dicom, write_minimal_dicom


class FakeStorageManager:
    def __init__(self, raise_on_save: Exception | None = None,
                 raise_on_process: Exception | None = None):
        self.saved: list[tuple] = []
        self.processed: list[tuple] = []
        self._raise_on_save = raise_on_save
        self._raise_on_process = raise_on_process

    def save_temp_image(self, ds, image_id):
        if self._raise_on_save:
            raise self._raise_on_save
        path = f"/fake/temp/{image_id}.dcm"
        self.saved.append((ds, image_id))
        return path

    def process_image(self, image_path, image_id):
        if self._raise_on_process:
            raise self._raise_on_process
        self.processed.append((image_path, image_id))


def make_dataset(with_uids: bool = True) -> pydicom.Dataset:
    ds = pydicom.Dataset()
    if with_uids:
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
        ds.SOPInstanceUID = "1.2.840.99999.1"
    return ds


def make_handler(storage=None) -> CStoreSCPHandler:
    return CStoreSCPHandler(Settings(), storage or FakeStorageManager())


class TestValidateDicom:
    def test_valid_when_both_uids_present(self):
        handler = make_handler()
        assert handler.validate_dicom(make_dataset(with_uids=True)) is True

    def test_invalid_when_sop_class_missing(self):
        handler = make_handler()
        ds = pydicom.Dataset()
        ds.SOPInstanceUID = "1.2.3"
        assert handler.validate_dicom(ds) is False

    def test_invalid_when_sop_instance_missing(self):
        handler = make_handler()
        ds = pydicom.Dataset()
        ds.SOPClassUID = "1.2.3"
        assert handler.validate_dicom(ds) is False

    def test_invalid_when_both_missing(self):
        handler = make_handler()
        assert handler.validate_dicom(pydicom.Dataset()) is False


class TestHandleCStore:
    def test_missing_dataset_key_returns_processing_failure(self):
        storage = FakeStorageManager()
        handler = make_handler(storage)
        status = handler.handle_c_store(assoc=None, context=None, info={})
        assert status == 0xC000
        assert storage.saved == []

    def test_invalid_dataset_returns_processing_failure_without_saving(self):
        storage = FakeStorageManager()
        handler = make_handler(storage)
        status = handler.handle_c_store(
            assoc=None, context=None, info={"dataset": make_dataset(with_uids=False)}
        )
        assert status == 0xC000
        assert storage.saved == []

    def test_valid_dataset_saves_then_processes_and_returns_success(self):
        storage = FakeStorageManager()
        handler = make_handler(storage)
        ds = make_dataset()

        status = handler.handle_c_store(assoc=None, context=None, info={"dataset": ds})

        assert status == 0x0000
        assert len(storage.saved) == 1
        assert len(storage.processed) == 1
        # Same image_id threaded through both calls.
        saved_id = storage.saved[0][1]
        processed_path, processed_id = storage.processed[0]
        assert saved_id == processed_id
        assert processed_path == f"/fake/temp/{saved_id}.dcm"

    def test_file_meta_attached_to_dataset_before_saving(self):
        storage = FakeStorageManager()
        handler = make_handler(storage)
        ds = make_dataset()
        file_meta = pydicom.dataset.FileMetaDataset()
        file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

        handler.handle_c_store(
            assoc=None, context=None, info={"dataset": ds, "file_meta": file_meta}
        )

        saved_ds, _id = storage.saved[0]
        assert saved_ds.file_meta is file_meta

    def test_empty_but_present_file_meta_is_still_attached(self):
        # Regression: `if file_meta:` used to check FileMetaDataset
        # truthiness, which follows element count — an empty-but-present
        # file_meta (len 0) would silently never get attached. Fixed to
        # `is not None`.
        storage = FakeStorageManager()
        handler = make_handler(storage)
        ds = make_dataset()
        empty_file_meta = pydicom.dataset.FileMetaDataset()
        assert len(empty_file_meta) == 0

        handler.handle_c_store(
            assoc=None, context=None, info={"dataset": ds, "file_meta": empty_file_meta}
        )

        saved_ds, _id = storage.saved[0]
        assert saved_ds.file_meta is empty_file_meta

    def test_no_file_meta_key_does_not_touch_dataset_file_meta(self):
        storage = FakeStorageManager()
        handler = make_handler(storage)
        ds = make_dataset()

        handler.handle_c_store(assoc=None, context=None, info={"dataset": ds})

        saved_ds, _id = storage.saved[0]
        assert not hasattr(saved_ds, "file_meta") or saved_ds.file_meta is None

    def test_save_failure_returns_out_of_resources(self):
        storage = FakeStorageManager(raise_on_save=OSError("disk full"))
        handler = make_handler(storage)
        status = handler.handle_c_store(assoc=None, context=None, info={"dataset": make_dataset()})
        assert status == 0x0106

    def test_process_failure_returns_out_of_resources_but_save_already_happened(self):
        storage = FakeStorageManager(raise_on_process=RuntimeError("boom"))
        handler = make_handler(storage)
        status = handler.handle_c_store(assoc=None, context=None, info={"dataset": make_dataset()})
        assert status == 0x0106
        assert len(storage.saved) == 1  # save_temp_image ran before process_image raised
        assert storage.processed == []

    def test_two_calls_generate_different_image_ids(self):
        storage = FakeStorageManager()
        handler = make_handler(storage)
        handler.handle_c_store(assoc=None, context=None, info={"dataset": make_dataset()})
        handler.handle_c_store(assoc=None, context=None, info={"dataset": make_dataset()})
        assert storage.saved[0][1] != storage.saved[1][1]


class TestHandleCStoreIntegration:
    """One end-to-end pass through the real StorageManager, to catch a
    signature/contract drift between CStoreSCPHandler and StorageManager
    that the FakeStorageManager tests above couldn't."""

    def test_real_storage_manager_organizes_the_image(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager

        settings = make_settings(tmp_path)
        storage = StorageManager(settings)
        handler = CStoreSCPHandler(settings, storage)

        src = tmp_path / "incoming.dcm"
        write_minimal_dicom(
            src,
            StudyInstanceUID="1.2.840.99999.100",
            SeriesInstanceUID="1.2.840.99999.200",
            PatientID="MRN1",
        )
        ds = read_dicom(src)

        status = handler.handle_c_store(
            assoc=None, context=None, info={"dataset": ds, "file_meta": ds.file_meta}
        )

        assert status == 0x0000
        organized = list(storage.base_path.rglob("*.dcm"))
        assert len(organized) == 1
        assert organized[0].parent.parent == storage.base_path / "0001"


class TestDicomServerConfig:
    def test_default_port(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))
        server = DicomServer(Settings(), storage)
        assert server.ae_port == 11112

    def test_configured_port(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))
        server = DicomServer(Settings(dicom_server={"port": 4242}), storage)
        assert server.ae_port == 4242

    def test_c_store_handler_created(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))
        server = DicomServer(Settings(), storage)
        assert isinstance(server.c_store_handler, CStoreSCPHandler)


class FakeEvent:
    def __init__(self, assoc=None, context=None, dataset=None, file_meta=None):
        self.assoc = assoc
        self.context = context
        self.dataset = dataset
        self.file_meta = file_meta


class TestDicomServerHandleEcho:
    def test_returns_success(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))
        server = DicomServer(Settings(), storage)
        assert server.handle_echo(FakeEvent()) == 0x0000


class TestDicomServerHandleCStore:
    """Tests the server-level event adapter, not CStoreSCPHandler itself
    (that's TestHandleCStore above) — verifies it correctly unpacks the
    pynetdicom Event into the (assoc, context, info) shape the handler
    expects, and maps a handler exception to 0x0106."""

    def _server(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))
        return DicomServer(Settings(), storage)

    def test_delegates_to_c_store_handler_with_unpacked_event(self, tmp_path):
        server = self._server(tmp_path)
        calls = []

        class FakeHandler:
            def handle_c_store(self, assoc, context, info):
                calls.append((assoc, context, info))
                return 0x0000

        server.c_store_handler = FakeHandler()
        event = FakeEvent(assoc="ASSOC", context="CTX", dataset="DS", file_meta="META")

        status = server.handle_c_store(event)

        assert status == 0x0000
        assert calls == [("ASSOC", "CTX", {"dataset": "DS", "file_meta": "META"})]

    def test_handler_exception_maps_to_out_of_resources(self, tmp_path):
        server = self._server(tmp_path)

        class RaisingHandler:
            def handle_c_store(self, assoc, context, info):
                raise RuntimeError("boom")

        server.c_store_handler = RaisingHandler()
        status = server.handle_c_store(FakeEvent())
        assert status == 0x0106


class TestDicomServerStart:
    def test_raises_oserror_when_port_already_bound(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))

        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(('', 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]

        try:
            server = DicomServer(
                Settings(dicom_server={"port": port, "ae_title": "TEST"}), storage
            )
            with pytest.raises(OSError):
                asyncio.run(server.start())
            # Failed before AE construction — nothing left half-initialized.
            assert server.ae is None
        finally:
            occupied.close()


class TestDicomServerStop:
    def test_stop_with_no_server_task_is_a_noop(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))
        server = DicomServer(Settings(), storage)
        asyncio.run(server.stop())  # must not raise
        assert server.server_task is None

    def test_stop_shuts_down_ae_and_clears_task(self, tmp_path):
        # stop() only calls stop_server()/ae.shutdown() when server_task is
        # still running (real life: it's blocked inside ae.start_server()
        # at that point) — a pre-completed future would skip that whole
        # branch, so the fake shutdown() has to actually resolve the future
        # itself, the way a real ae.shutdown() unblocks start_server().
        from pixieveil.storage.storage_manager import StorageManager
        storage = StorageManager(make_settings(tmp_path))
        server = DicomServer(Settings(), storage)

        shutdown_calls = []

        async def run():
            loop = asyncio.get_running_loop()
            server.server_task = loop.create_future()

            class FakeAE:
                def shutdown(self):
                    shutdown_calls.append(True)
                    loop.call_soon_threadsafe(server.server_task.set_result, None)

            server.ae = FakeAE()
            await server.stop()

        asyncio.run(run())

        assert shutdown_calls == [True]
        assert server.server_task is None
        assert server.ae is None
