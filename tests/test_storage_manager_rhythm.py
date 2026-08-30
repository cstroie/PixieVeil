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
    def __init__(self, result: RhythmUploadResult, *, enabled=True, site_code="S001"):
        self.enabled = enabled
        self.site_code = site_code
        self._result = result
        self.calls: list[dict] = []

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


OK_RESULT = RhythmUploadResult(
    ok=True, status="COMPLETE", session_id="sess-1",
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
    def test_first_export_archives_and_cleans_up(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm)
        study_dir = sm.base_path / "0001"

        r = asyncio.run(sm.manual_upload_rhythm(1))

        assert r["ok"] is True
        assert r["job_id"] == "job-1"
        assert r["batch"].startswith("S001-")
        # bookkeeping
        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "archived"
        assert sc.archived_via == "rhythm"
        assert sc.rhythm_job_id == "job-1"
        assert sc.rhythm_batch == r["batch"]
        assert sc.rhythm_status == "COMPLETE"
        # retain not configured -> study dir removed
        assert not study_dir.exists()
        assert sm.get_counter("export", "rhythm_studies") == 1
        assert sm.get_counter("export", "rhythm_images") == 2

    def test_dose_report_series_excluded_from_upload(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm, sc_report=True)

        r = asyncio.run(sm.manual_upload_rhythm(1))

        assert r["ok"] is True
        # 2 CT images counted, the SC dose-report object not
        assert sm.get_counter("export", "rhythm_images") == 2

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

    def test_resend_of_archived_study_does_not_re_move_files(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        _ready_study(sm)
        sm.find_sidecar_by_number(1).status = "archived"
        study_dir = sm.base_path / "0001"

        r = asyncio.run(sm.manual_upload_rhythm(1))

        assert r["ok"] is True
        assert study_dir.exists()  # not deleted on a re-send
        assert sm.get_counter("cleanup", "studies") == 0


class TestFailure:
    def test_job_failed_persists_status_and_keeps_study(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage = FakeRhythm(
            RhythmUploadResult(
                ok=False, status="FAILED", session_id="sess-2",
                job_id="job-2", job_status_url="/api/v1/uploads/job-2/",
                error="job ended FAILED",
                error_report=[{"code": "gdpr", "message": "PatientName present"}],
            )
        )
        _ready_study(sm)
        study_dir = sm.base_path / "0001"

        r = asyncio.run(sm.manual_upload_rhythm(1))

        assert r["ok"] is False
        assert "RHYTHM upload failed" in r["message"]
        assert r["error_report"] == [{"code": "gdpr", "message": "PatientName present"}]
        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "ready"          # NOT archived
        assert sc.archived_via is None
        assert sc.rhythm_status == "FAILED"
        assert sc.rhythm_job_id == "job-2"
        assert study_dir.exists()
        assert sm.get_counter("export", "errors") == 1


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
