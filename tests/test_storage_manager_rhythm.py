"""Unit tests for StorageManager.manual_upload_rhythm / _batch and the
RHYTHM-specific helpers (_rhythm_excluded_series_sync, batch-id).

The chunked transport (RhythmStorage.upload_study) has its own suite in
test_rhythm_storage.py; here it is replaced with a small fake that records
its arguments and returns a canned RhythmUploadResult, so nothing touches
the network.
"""

import asyncio

from pydicom.uid import (
    CTImageStorage,
    SecondaryCaptureImageStorage,
    XRayRadiationDoseSRStorage,
)

from pixieveil.storage.storage_manager import StorageManager
from pixieveil.storage.rhythm_storage import RhythmUploadResult

from .conftest import make_settings, write_minimal_dicom
from .test_storage_manager import register_study


EXAM_OK = {
    "study_number": 1,
    "indication": {
        "region": "Head",
        "clinical_indication": "Trauma",
        "iv_contrast": "Non-contrast",
    },
    "protocol_type": "PEDIATRIC_HEAD",
    "examination_group": "Group 4 - Childhood",
    "scanner": {"rhythm_scanner_id": "scanner-1"},
    "protocol_used_hint": {"protocol_name": "HeadRoutine"},
    "patient": {"age_years": 6, "weight_kg": 20.0},
    "rdsr": None,
    "series": [],
    "image_quality": "Acceptable",
}


class FakeRhythm:
    def __init__(self, result: RhythmUploadResult = None, *,
                 job_result: RhythmUploadResult = None,
                 enabled=True, site_code="S001"):
        self.enabled = enabled
        self.site_code = site_code
        self._result = result or RhythmUploadResult(
            ok=True, session_id="sess-1", job_id="job-1",
            job_status_url="/api/v1/uploads/job-1/",
        )
        self._job_result = job_result
        self.calls: list[dict] = []
        self.job_checks: list[str] = []

    async def upload_study(self, zip_path, manifest_item, batch, *, file_hash=None):
        self.calls.append(
            {
                "zip_exists": zip_path.exists(),
                "item": dict(manifest_item),
                "batch": batch,
                "file_hash": file_hash,
            }
        )
        return self._result

    async def check_job(self, job_status_url):
        self.job_checks.append(job_status_url)
        return self._job_result


def _manager(tmp_path, *, profile="gdpr", rhythm_configured=True):
    storage = {
        "base_path": str(tmp_path / "data"),
        "temp_path": str(tmp_path / "tmp"),
    }
    if rhythm_configured:
        storage["remote_storage"] = {
            "rhythm": {
                "base_url": "https://rhythm.example",
                "auth_token": "tok",
                "site_code": "S001",
            }
        }
    return StorageManager(
        make_settings(
            tmp_path,
            storage=storage,
            # Anonymizer validates profile against profiles{} at construction;
            # the bodies don't matter here, only which name is selected.
            anonymization={"profile": profile, "profiles": {"gdpr": {}, "research": {}}},
        )
    )


def _ready_study(sm, *, with_exam=EXAM_OK, ct=True, sc_report=False):
    register_study(sm, 1, "ready")
    if ct:
        write_minimal_dicom(
            sm.base_path / "0001" / "0001" / "img1.dcm", SOPClassUID=CTImageStorage
        )
        write_minimal_dicom(
            sm.base_path / "0001" / "0001" / "img2.dcm", SOPClassUID=CTImageStorage
        )
    if sc_report:
        write_minimal_dicom(
            sm.base_path / "0001" / "0009" / "dose.dcm",
            SOPClassUID=SecondaryCaptureImageStorage,
        )
    if with_exam is not None:
        from pixieveil.storage.exam_sidecar import ExamSidecar

        ExamSidecar(1, dict(with_exam)).save(sm.base_path)


# upload_study now returns once complete/ succeeds (job queued, not ingested).
OK_RESULT = RhythmUploadResult(
    ok=True, session_id="sess-1",
    job_id="job-1", job_status_url="/api/v1/uploads/job-1/",
)


class TestGuards:
    def test_missing_study_dir(self, tmp_path):
        sm = _manager(tmp_path)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r == {"ok": False, "message": "study directory not found"}

    def test_not_configured(self, tmp_path):
        sm = _manager(tmp_path, rhythm_configured=False)
        (sm.base_path / "0001").mkdir(parents=True)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False and "not configured" in r["message"]

    def test_wrong_profile_refused(self, tmp_path):
        sm = _manager(tmp_path, profile="research")
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        (sm.base_path / "0001").mkdir(parents=True)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False
        assert "requires the 'gdpr'" in r["message"]

    def test_defacing_failed_refused(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        register_study(sm, 1, "defacing_failed")
        (sm.base_path / "0001").mkdir(parents=True)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False and "defacing did not complete" in r["message"]

    def test_missing_exam_sidecar(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm, with_exam=None)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False and "not been extracted" in r["message"]

    def test_blocker_when_image_quality_unset(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        exam = dict(EXAM_OK, image_quality=None)
        _ready_study(sm, with_exam=exam)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False
        assert "image_quality is not set" in r["message"]
        assert not sm.rhythm_storage.calls  # never reached the transport

    def test_no_ct_series(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm, ct=False, sc_report=True)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False and "no CT image series" in r["message"]

    def test_concurrent_action_rejected(self, tmp_path):
        sm = _manager(tmp_path)
        sm._manual_actions_in_progress.add(1)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r == {"ok": False, "message": "action already in progress for this study"}


class TestHappyPath:
    def test_upload_queues_job_without_archiving(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm)
        study_dir = sm.base_path / "0001"

        r = asyncio.run(sm.manual_upload_rhythm(1))

        assert r["ok"] is True
        assert r["status"] == "uploaded"
        assert r["job_id"] == "job-1"
        assert r["batch"].startswith("S001-")
        # job is queued, NOT finalized — that waits for reconcile on COMPLETE
        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "ready"
        assert sc.archived_via is None
        assert sc.rhythm_job_id == "job-1"
        assert sc.rhythm_job_status_url == "/api/v1/uploads/job-1/"
        assert sc.rhythm_batch == r["batch"]
        assert sc.rhythm_status == "uploaded"
        assert study_dir.exists()
        assert sm.get_counter("export", "rhythm_studies") == 0

    def test_manifest_item_passed_to_transport(self, tmp_path):
        sm = _manager(tmp_path)
        fake = FakeRhythm(OK_RESULT)
        sm.rhythm_storage = fake
        _ready_study(sm)

        asyncio.run(sm.manual_upload_rhythm(1))

        call = fake.calls[0]
        assert call["zip_exists"] is True
        assert call["item"]["clinical_indication_code"] == "HEADTRAUMA"
        assert call["item"]["patient_group_code"] == "PH-G4"
        assert call["item"]["image_quality"] == "Acceptable"
        assert call["item"]["filename"] == "0001.zip"
        # sha256/size_bytes are filled in from the real ZIP before upload —
        # RHYTHM verifies the checksum before it extracts the archive.
        assert len(call["item"]["sha256"]) == 64
        assert all(c in "0123456789abcdef" for c in call["item"]["sha256"])
        assert call["item"]["size_bytes"] > 0
        # same hash handed to the transport so it isn't recomputed
        assert call["file_hash"] == call["item"]["sha256"]

    def test_resend_of_archived_study_still_just_queues(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm)
        sm.find_sidecar_by_number(1).status = "archived"
        study_dir = sm.base_path / "0001"

        r = asyncio.run(sm.manual_upload_rhythm(1))

        assert r["ok"] is True
        assert study_dir.exists()
        assert sm.get_counter("cleanup", "studies") == 0


class TestUploadFailure:
    def test_transport_failure_persists_failed_status_and_keeps_study(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(
            RhythmUploadResult(
                ok=False, session_id="sess-2",
                error="complete failed: HTTP 503",
                error_report=[{"code": "x", "message": "y"}],
            )
        )
        _ready_study(sm)
        study_dir = sm.base_path / "0001"

        r = asyncio.run(sm.manual_upload_rhythm(1))

        assert r["ok"] is False
        assert "RHYTHM upload failed" in r["message"]
        assert r["error_report"] == [{"code": "x", "message": "y"}]
        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "ready"          # NOT archived
        assert sc.archived_via is None
        assert sc.rhythm_status == "failed"
        assert study_dir.exists()
        assert sm.get_counter("export", "errors") == 1


class TestReconcile:
    def _study_with_pending_job(self, sm):
        sc = register_study(sm, 1, "ready")
        write_minimal_dicom(
            sm.base_path / "0001" / "0001" / "a.dcm", SOPClassUID=CTImageStorage
        )
        write_minimal_dicom(
            sm.base_path / "0001" / "0001" / "b.dcm", SOPClassUID=CTImageStorage
        )
        sm.study_manager.set_rhythm_job(
            sc.original_study_uid, batch="S001-x", job_id="job-1",
            job_status_url="/api/v1/uploads/job-1/", status="uploaded",
        )
        return sc

    def test_complete_finalizes_retain_delete_and_archive(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(
            job_result=RhythmUploadResult(ok=True, status="COMPLETE")
        )
        self._study_with_pending_job(sm)
        study_dir = sm.base_path / "0001"

        asyncio.run(sm.reconcile_rhythm_jobs())

        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "archived"
        assert sc.archived_via == "rhythm"
        assert sc.rhythm_status == "COMPLETE"
        assert not study_dir.exists()
        assert sm.get_counter("export", "rhythm_studies") == 1
        assert sm.get_counter("export", "rhythm_images") == 2
        assert sm.get_counter("cleanup", "studies") == 1

    def test_failed_records_state_without_archiving(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(
            job_result=RhythmUploadResult(
                ok=False, status="FAILED", error="job ended FAILED",
                error_report=[{"code": "gdpr"}],
            )
        )
        self._study_with_pending_job(sm)

        asyncio.run(sm.reconcile_rhythm_jobs())

        sc = sm.find_sidecar_by_number(1)
        assert sc.rhythm_status == "FAILED"
        assert sc.status == "ready"
        assert sc.archived_via is None
        assert (sm.base_path / "0001").exists()

    def test_pending_job_is_left_alone(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(
            job_result=RhythmUploadResult(ok=False, status="PROCESSING")
        )
        self._study_with_pending_job(sm)

        asyncio.run(sm.reconcile_rhythm_jobs())

        sc = sm.find_sidecar_by_number(1)
        assert sc.rhythm_status == "uploaded"
        assert sc.status == "ready"

    def test_terminal_job_is_not_re_polled(self, tmp_path):
        sm = _manager(tmp_path)
        fake = FakeRhythm(job_result=RhythmUploadResult(ok=True, status="COMPLETE"))
        sm.rhythm_storage = fake
        sc = self._study_with_pending_job(sm)
        sm.study_manager.set_rhythm_job(sc.original_study_uid, status="COMPLETE")

        asyncio.run(sm.reconcile_rhythm_jobs())

        assert fake.job_checks == []

    def test_noop_when_rhythm_disabled(self, tmp_path):
        sm = _manager(tmp_path, rhythm_configured=False)
        fake = FakeRhythm(job_result=RhythmUploadResult(ok=True, status="COMPLETE"),
                          enabled=False)
        sm.rhythm_storage = fake
        self._study_with_pending_job(sm)

        asyncio.run(sm.reconcile_rhythm_jobs())

        assert fake.job_checks == []


class TestBatch:
    def test_batch_shares_one_id_across_studies(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm)
        register_study(sm, 2, "ready")
        write_minimal_dicom(
            sm.base_path / "0002" / "0001" / "i.dcm", SOPClassUID=CTImageStorage
        )
        from pixieveil.storage.exam_sidecar import ExamSidecar

        ExamSidecar(2, dict(EXAM_OK, study_number=2)).save(sm.base_path)

        r = asyncio.run(sm.manual_upload_rhythm_batch([1, 2]))

        assert r["ok"] is True
        assert set(r["results"]) == {"1", "2"}
        batches = {c["batch"] for c in sm.rhythm_storage.calls}
        assert batches == {r["batch"]}


class TestManifestPreview:
    def test_full_study_previews_clean_without_checksum_fields(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)

        out = sm.build_rhythm_manifest_preview_sync([1])

        m = out["manifest"]
        assert m["type"] == "rhythm_server_assigned_upload_manifest"
        assert m["site"] == "S001"
        assert m["batch"].startswith("S001-")
        assert len(m["items"]) == 1
        item = m["items"][0]
        assert item["ref"] == "ROW0001"
        assert item["clinical_indication_code"] == "HEADTRAUMA"
        # sha256/size_bytes come from the export ZIP at upload time
        assert "sha256" not in item and "size_bytes" not in item
        assert out["blockers"] == {}

    def test_blockers_reported_but_item_still_included(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm, with_exam=dict(EXAM_OK, image_quality=None))

        out = sm.build_rhythm_manifest_preview_sync([1])

        assert len(out["manifest"]["items"]) == 1
        assert out["blockers"]["0001"] == ["image_quality is not set"]

    def test_missing_exam_sidecar_is_a_blocker_not_an_item(self, tmp_path):
        sm = _manager(tmp_path)
        register_study(sm, 2, "ready")  # no exam sidecar written

        out = sm.build_rhythm_manifest_preview_sync([2])

        assert out["manifest"]["items"] == []
        assert out["blockers"]["0002"] == ["exam data has not been extracted yet"]

    def test_multi_study_shares_one_batch(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)
        register_study(sm, 2, "ready")
        from pixieveil.storage.exam_sidecar import ExamSidecar
        ExamSidecar(2, dict(EXAM_OK, study_number=2)).save(sm.base_path)

        out = sm.build_rhythm_manifest_preview_sync([1, 2])

        assert [i["ref"] for i in out["manifest"]["items"]] == ["ROW0001", "ROW0002"]

    def test_site_code_falls_back_when_rhythm_unconfigured(self, tmp_path):
        sm = _manager(tmp_path, rhythm_configured=False)
        _ready_study(sm)

        out = sm.build_rhythm_manifest_preview_sync([1])

        assert out["site_code"] == "SITE"
        assert out["manifest"]["site"] == "SITE"


class TestExcludedSeriesHelper:
    def test_only_ct_series_kept(self, tmp_path):
        sm = _manager(tmp_path)
        write_minimal_dicom(
            sm.base_path / "0001" / "0001" / "a.dcm", SOPClassUID=CTImageStorage
        )
        write_minimal_dicom(
            sm.base_path / "0001" / "0009" / "b.dcm",
            SOPClassUID=SecondaryCaptureImageStorage,
        )
        write_minimal_dicom(
            sm.base_path / "0001" / "0010" / "c.dcm",
            SOPClassUID=XRayRadiationDoseSRStorage,  # native RDSR
        )
        excluded = sm._rhythm_excluded_series_sync(sm.base_path / "0001")
        assert excluded == {"0009", "0010"}

    def test_batch_id_uses_site_code(self, tmp_path):
        sm = _manager(tmp_path)
        assert sm._new_rhythm_batch_id().startswith("S001-")
