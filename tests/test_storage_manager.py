"""Unit tests for StorageManager: study-dir resolution, quota enforcement,
the DICOM patient-field write-back, and the manual-export "not configured"
guard rails. Export/upload/defacing stay disabled throughout (see
conftest.make_settings), so nothing here touches the network or a GPU.
"""

import asyncio

from pixieveil.processing.exam_extractor import _parse_dicom_age
from pixieveil.processing.study_manager import StudyState
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.storage_manager import StorageManager
from pixieveil.storage.study_sidecar import StudySidecar

from .conftest import read_dicom, write_minimal_dicom


def register_study(sm: StorageManager, study_number: int, status: str,
                    archived_via=None) -> StudySidecar:
    """Register a study's sidecar in the in-memory index (StudyManager
    normally does this via add_image_to_study/record_new_series; tests set
    up the end state directly instead of replaying the whole ingest path)."""
    uid = f"study-uid-{study_number}"
    sc = StudySidecar.create(
        study_number=study_number,
        original_study_uid=uid,
        original_patient_id="pid",
        anonymized_study_uid="anon-uid",
        anonymized_patient_id="anon-pid",
    )
    sc.status = status
    sc.archived_via = archived_via
    sm.study_manager._sidecars[uid] = sc
    sm.study_manager.study_map[uid] = study_number
    sm.study_manager.number_map[study_number] = uid
    sc.save(sm.base_path)
    return sc


class TestResolveStudyDir:
    def test_absent_when_neither_location_has_it(self, storage_manager):
        path, location = storage_manager.resolve_study_dir(1)
        assert path is None
        assert location == "absent"

    def test_found_under_base_path(self, storage_manager):
        study_dir = storage_manager.base_path / "0001"
        study_dir.mkdir(parents=True)
        path, location = storage_manager.resolve_study_dir(1)
        assert path == study_dir
        assert location == "base"

    def test_found_under_retained_path_when_not_in_base(self, storage_manager):
        retained_dir = storage_manager.retained_path / "0002"
        retained_dir.mkdir(parents=True)
        path, location = storage_manager.resolve_study_dir(2)
        assert path == retained_dir
        assert location == "retained"

    def test_base_path_takes_priority_over_retained(self, storage_manager):
        base_dir = storage_manager.base_path / "0003"
        base_dir.mkdir(parents=True)
        (storage_manager.retained_path / "0003").mkdir(parents=True)
        path, location = storage_manager.resolve_study_dir(3)
        assert path == base_dir
        assert location == "base"


class TestFindSidecarByNumber:
    def test_delegates_to_study_manager_index(self, storage_manager):
        register_study(storage_manager, 1, "ready")
        sc = storage_manager.find_sidecar_by_number(1)
        assert sc is not None
        assert sc.original_study_uid == "study-uid-1"

    def test_unknown_number_returns_none(self, storage_manager):
        assert storage_manager.find_sidecar_by_number(999) is None


class TestEnforceStorageQuota:
    def _tiny_quota_manager(self, tmp_path):
        from .conftest import make_settings
        sm = StorageManager(make_settings(tmp_path, storage={
            "base_path": str(tmp_path / "data"),
            "temp_path": str(tmp_path / "tmp"),
            "max_storage_gb": 1e-8,  # ~11 bytes — trivially exceeded by any real file
        }))
        return sm

    def test_ready_study_is_never_purged_even_when_quota_exceeded(self, tmp_path):
        sm = self._tiny_quota_manager(tmp_path)
        register_study(sm, 1, "ready")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img1.dcm")

        sm.enforce_storage_quota_sync()

        assert (sm.base_path / "0001").exists()

    def test_archived_study_is_purged_when_quota_exceeded(self, tmp_path):
        sm = self._tiny_quota_manager(tmp_path)
        register_study(sm, 1, "archived", archived_via="dicom")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img1.dcm")

        sm.enforce_storage_quota_sync()

        assert not (sm.base_path / "0001").exists()

    def test_active_study_is_never_purged_even_if_archived_status(self, tmp_path):
        # Shouldn't happen in practice (archived studies are inactive by
        # construction), but active-ness is meant to be an absolute guard —
        # verify it wins even against a stale/inconsistent status.
        sm = self._tiny_quota_manager(tmp_path)
        register_study(sm, 1, "archived", archived_via="dicom")
        sm.study_manager.study_states["study-uid-1"] = StudyState()
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img1.dcm")

        sm.enforce_storage_quota_sync()

        assert (sm.base_path / "0001").exists()

    def test_no_op_when_max_storage_gb_not_configured(self, storage_manager):
        register_study(storage_manager, 1, "archived", archived_via="dicom")
        write_minimal_dicom(storage_manager.base_path / "0001" / "0001" / "img1.dcm")

        storage_manager.enforce_storage_quota_sync()  # must not raise / not purge

        assert (storage_manager.base_path / "0001").exists()


class TestWritebackPatientFields:
    def _study_with_two_images(self, storage_manager, study_number=1):
        study_dir = storage_manager.base_path / f"{study_number:04d}"
        p1 = study_dir / "0001" / "img1.dcm"
        p2 = study_dir / "0001" / "img2.dcm"
        write_minimal_dicom(p1, PatientWeight=10.0, PatientAge="005Y")
        write_minimal_dicom(p2, PatientWeight=10.0, PatientAge="005Y")
        return study_dir, p1, p2

    def test_weight_and_age_written_to_every_file(self, storage_manager):
        study_dir, p1, p2 = self._study_with_two_images(storage_manager)
        storage_manager._writeback_patient_fields_sync(study_dir, 22.5, 8.0)

        for p in (p1, p2):
            ds = read_dicom(p)
            assert float(ds.PatientWeight) == 22.5
            assert _parse_dicom_age(ds.PatientAge) == 8.0

    def test_pre_deface_backup_dir_is_skipped(self, storage_manager):
        study_dir, p1, _ = self._study_with_two_images(storage_manager)
        backup = study_dir / "0001_pre_deface" / "img1.dcm"
        write_minimal_dicom(backup, PatientWeight=10.0, PatientAge="005Y")

        storage_manager._writeback_patient_fields_sync(study_dir, 22.5, None)

        assert float(read_dicom(p1).PatientWeight) == 22.5
        assert float(read_dicom(backup).PatientWeight) == 10.0  # untouched

    def test_implausible_weight_is_rejected_not_written(self, storage_manager):
        study_dir, p1, _ = self._study_with_two_images(storage_manager)
        storage_manager._writeback_patient_fields_sync(study_dir, -5.0, None)
        assert float(read_dicom(p1).PatientWeight) == 10.0  # unchanged

    def test_implausible_age_is_rejected_not_written(self, storage_manager):
        study_dir, p1, _ = self._study_with_two_images(storage_manager)
        storage_manager._writeback_patient_fields_sync(study_dir, None, 1500.0)
        assert read_dicom(p1).PatientAge == "005Y"  # unchanged

    def test_only_weight_given_leaves_age_untouched(self, storage_manager):
        study_dir, p1, _ = self._study_with_two_images(storage_manager)
        storage_manager._writeback_patient_fields_sync(study_dir, 30.0, None)
        ds = read_dicom(p1)
        assert float(ds.PatientWeight) == 30.0
        assert ds.PatientAge == "005Y"

    def test_corrupt_file_does_not_abort_the_rest(self, storage_manager):
        study_dir, p1, p2 = self._study_with_two_images(storage_manager)
        bogus = study_dir / "0001" / "bogus.dcm"
        bogus.write_bytes(b"not a dicom file")

        storage_manager._writeback_patient_fields_sync(study_dir, 22.5, None)

        assert float(read_dicom(p1).PatientWeight) == 22.5
        assert float(read_dicom(p2).PatientWeight) == 22.5


class TestSaveExamWriteback:
    def _setup_ready_study(self, storage_manager, study_number=1):
        study_dir = storage_manager.base_path / f"{study_number:04d}"
        dcm_path = study_dir / "0001" / "img1.dcm"
        write_minimal_dicom(dcm_path, PatientWeight=10.0, PatientAge="005Y")
        register_study(storage_manager, study_number, "ready")
        ExamSidecar(study_number, {
            "study_number": study_number,
            "patient": {"weight_kg": None, "age_years": None},
            "indication": {"region": None, "clinical_indication": None},
            "notes": [],
        }).save(storage_manager.base_path)
        return dcm_path

    def test_missing_exam_sidecar_returns_not_found(self, storage_manager):
        result = asyncio.run(storage_manager.save_exam(1, {"patient.weight_kg": 20.0}))
        assert result == {"ok": False, "message": "exam sidecar not found"}

    def test_weight_edit_reaches_both_sidecar_and_dicom_files(self, storage_manager):
        dcm_path = self._setup_ready_study(storage_manager)
        result = asyncio.run(storage_manager.save_exam(1, {"patient.weight_kg": 25.0}))

        assert result["ok"] is True
        assert result["data"]["patient"]["weight_kg"] == 25.0
        assert float(read_dicom(dcm_path).PatientWeight) == 25.0

    def test_archived_study_gets_sidecar_update_but_not_dicom_writeback(self, storage_manager):
        dcm_path = self._setup_ready_study(storage_manager)
        storage_manager.find_sidecar_by_number(1).status = "archived"

        result = asyncio.run(storage_manager.save_exam(1, {"patient.weight_kg": 25.0}))

        assert result["ok"] is True
        assert result["data"]["patient"]["weight_kg"] == 25.0
        assert float(read_dicom(dcm_path).PatientWeight) == 10.0  # untouched


class TestManualExportGuards:
    def test_send_dicom_missing_study_dir(self, storage_manager):
        result = asyncio.run(storage_manager.manual_send_dicom(1))
        assert result == {"ok": False, "message": "study directory not found"}

    def test_send_dicom_not_configured(self, storage_manager):
        (storage_manager.base_path / "0001").mkdir(parents=True)
        result = asyncio.run(storage_manager.manual_send_dicom(1))
        assert result == {"ok": False, "message": "DICOM export is not configured"}

    def test_upload_http_missing_study_dir(self, storage_manager):
        result = asyncio.run(storage_manager.manual_upload_http(1))
        assert result == {"ok": False, "message": "study directory not found"}

    def test_upload_http_not_configured(self, storage_manager):
        (storage_manager.base_path / "0001").mkdir(parents=True)
        result = asyncio.run(storage_manager.manual_upload_http(1))
        assert result == {"ok": False, "message": "HTTP export is not configured"}

    def test_concurrent_action_on_same_study_is_rejected(self, storage_manager):
        storage_manager._manual_actions_in_progress.add(1)
        result = asyncio.run(storage_manager.manual_send_dicom(1))
        assert result == {"ok": False, "message": "action already in progress for this study"}
