"""Re-send detection and the 'Allow re-receive' re-open.

Anonymization gives every image fresh UIDs, so a modality sending the same
study twice used to land every slice twice in one series (studies 0004/0005
on 2026-11-13), which then broke defacing. StudyManager.admit_image() keys
on the *original* SOPInstanceUID before anything is stored.
"""

import asyncio
from pathlib import Path

from pydicom.uid import generate_uid

from pixieveil.processing.study_manager import RECEIVED_SOPS_FILE, StudyManager
from pixieveil.storage.study_sidecar import StudySidecar

from .conftest import make_settings, write_minimal_dicom
from .test_dashboard import _request, make_dashboard
from .test_study_manager import make_study_manager


def _raw(sm, name: str, study_uid: str, series_uid: str, sop_uid: str) -> Path:
    """A received (not yet anonymized) image in temp_path."""
    sm.temp_path.mkdir(parents=True, exist_ok=True)
    p = sm.temp_path / f"{name}.dcm"
    write_minimal_dicom(p, StudyInstanceUID=study_uid, SeriesInstanceUID=series_uid,
                        PatientID="REAL-PATIENT-42")
    import pydicom
    ds = pydicom.dcmread(p)
    ds.SOPInstanceUID = sop_uid
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.save_as(p)
    return p


def _stored(sm) -> list[Path]:
    return sorted(sm.base_path.rglob("*.dcm"))


class TestAdmitImage:
    def test_new_images_admitted_duplicate_refused(self, tmp_path):
        sm = make_study_manager(tmp_path)
        assert sm.admit_image("S", "1.1") is None
        assert sm.admit_image("S", "1.2") is None
        assert sm.admit_image("S", "1.1") == "duplicate"

    def test_same_sop_in_another_study_is_independent(self, tmp_path):
        sm = make_study_manager(tmp_path)
        assert sm.admit_image("S1", "1.1") is None
        assert sm.admit_image("S2", "1.1") is None

    def test_release_allows_a_later_retry(self, tmp_path):
        sm = make_study_manager(tmp_path)
        assert sm.admit_image("S", "1.1") is None
        sm.release_image("S", "1.1")
        assert sm.admit_image("S", "1.1") is None

    def test_completed_study_refuses_everything(self, tmp_path):
        sm = make_study_manager(tmp_path, completion_timeout=0)
        sm.add_image_to_study("S", "SER")
        sm.check_study_completions()
        assert sm.admit_image("S", "never-seen") == "finished"


class TestStorageManagerResend:
    def test_study_sent_twice_is_stored_once(self, storage_manager):
        sm = storage_manager
        study, series = generate_uid(), generate_uid()
        sops = [generate_uid() for _ in range(3)]
        for send in range(2):
            for i, sop in enumerate(sops):
                sm.process_image(_raw(sm, f"s{send}-{i}", study, series, sop), f"s{send}-{i}")
        assert len(_stored(sm)) == 3
        assert sm.get_counter("reception", "duplicate_images") == 3
        assert not list(sm.temp_path.glob("*.dcm"))
        sm.study_manager.flush_resend_counts()
        sc = sm.study_manager.get_sidecar_by_uid(study)
        assert sc.resends_dropped == 3 and sc.last_resend_at

    def test_duplicate_detected_after_restart(self, tmp_path):
        from pixieveil.storage.storage_manager import StorageManager
        settings = make_settings(tmp_path)
        sm = StorageManager(settings)
        study, series, sop = generate_uid(), generate_uid(), generate_uid()
        sm.process_image(_raw(sm, "a", study, series, sop), "a")
        ledger = sm.base_path / "0001" / RECEIVED_SOPS_FILE
        assert ledger.exists() and sop not in ledger.read_text()  # hashed

        sm2 = StorageManager(settings)
        sm2.process_image(_raw(sm2, "b", study, series, sop), "b")
        assert len(_stored(sm2)) == 1
        assert sm2.get_counter("reception", "duplicate_images") == 1

    def test_failed_store_releases_reservation(self, storage_manager, monkeypatch):
        sm = storage_manager
        study, series, sop = generate_uid(), generate_uid(), generate_uid()

        def boom(ds):
            raise RuntimeError("anonymizer broke")
        original = sm.anonymizer.anonymize
        monkeypatch.setattr(sm.anonymizer, "anonymize", boom)
        sm.process_image(_raw(sm, "a", study, series, sop), "a")
        monkeypatch.setattr(sm.anonymizer, "anonymize", original)
        sm.process_image(_raw(sm, "b", study, series, sop), "b")
        assert len(_stored(sm)) == 1

    def test_image_for_finished_study_is_dropped(self, storage_manager):
        sm = storage_manager
        study, series = generate_uid(), generate_uid()
        sm.process_image(_raw(sm, "a", study, series, generate_uid()), "a")
        sm.study_manager.mark_study_ready(study)
        sm.process_image(_raw(sm, "b", study, series, generate_uid()), "b")
        assert len(_stored(sm)) == 1
        assert sm.get_counter("reception", "finished_images") == 1


class TestReopen:
    def _ready_study(self, sm, *, status="archived"):
        study, series = generate_uid(), generate_uid()
        sop = generate_uid()
        sm.process_image(_raw(sm, "a", study, series, sop), "a")
        smgr: StudyManager = sm.study_manager
        sc = smgr.get_sidecar_by_uid(study)
        smgr.set_series_classification(study, series, is_head=True, is_topogram=False)
        smgr.mark_series_defaced(study, series)
        smgr._stamp_sidecar(study, status=status, archived_via="rhythm",
                            exported_at="2026-11-13T10:00:00+00:00",
                            rhythm_job_id="job-1", rhythm_status="COMPLETE",
                            rhythm_repository_study_id="RS-1")
        sm.export_path.mkdir(parents=True, exist_ok=True)
        (sm.export_path / "0001.zip").write_bytes(b"zip")
        return study, series, sop, sc

    def test_reopen_deletes_files_and_resets_state(self, storage_manager):
        sm = storage_manager
        study, series, sop, sc = self._ready_study(sm)
        result = asyncio.run(sm.reopen_study(1))
        assert result["ok"], result
        assert not (sm.base_path / "0001").exists()
        assert not (sm.export_path / "0001.zip").exists()
        assert sc.status == "receiving" and sc.reopened_at
        assert sc.archived_via is None and sc.exported_at is None
        assert sc.rhythm_job_id is None and sc.rhythm_status is None
        assert sc.rhythm_repository_study_id == "RS-1"  # history kept
        rec = sc.series[series]
        assert rec.defaced is False and rec.is_head is False

        # the same image is now accepted again, into the same numbers
        sm.process_image(_raw(sm, "b", study, series, sop), "b")
        assert [p.relative_to(sm.base_path).as_posix() for p in _stored(sm)] == [
            "0001/0001/0001.dcm"]
        on_disk = StudySidecar.load(StudySidecar.path_for(sm.base_path, 1))
        assert on_disk.status == "receiving"

    def test_reopen_refused_while_processing(self, storage_manager):
        sm = storage_manager
        study, *_ = self._ready_study(sm, status="defacing")
        result = asyncio.run(sm.reopen_study(1))
        assert not result["ok"] and "defacing" in result["message"]
        assert (sm.base_path / "0001").exists()

    def test_reopen_refused_with_rhythm_upload_in_flight(self, storage_manager):
        sm = storage_manager
        study, *_ = self._ready_study(sm, status="ready")
        sm.study_manager._stamp_sidecar(study, rhythm_status="PROCESSING")
        result = asyncio.run(sm.reopen_study(1))
        assert not result["ok"] and "RHYTHM" in result["message"]

    def test_reopen_unknown_study(self, storage_manager):
        result = asyncio.run(storage_manager.reopen_study(99))
        assert not result["ok"]


class TestReopenRoute:
    def test_admin_can_reopen(self, tmp_path):
        d = make_dashboard(make_settings(tmp_path))
        TestReopen()._ready_study(d.app["storage_manager"])
        status, body = _request(d, "POST", "/api/studies/1/actions/reopen")
        assert status == 200, body
        assert body["status"] == "receiving"

    def test_non_admin_is_forbidden(self, tmp_path):
        d = make_dashboard(make_settings(tmp_path))
        TestReopen()._ready_study(d.app["storage_manager"])
        d.user_store.add("bob", "bob-password", admin=False)
        token, _ = d.user_store.issue_token("bob", "tests")
        status, _ = _request(d, "POST", "/api/studies/1/actions/reopen", auth=False,
                             headers={"Authorization": f"Bearer {token}"})
        assert status == 403
        assert (d.app["storage_manager"].base_path / "0001").exists()
