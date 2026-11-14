import pytest
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


def _iso_ago(seconds: float) -> str:
    """ISO-8601 timestamp `seconds` in the past — for StudySidecar.exported_at."""
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc).astimezone()
            - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _json_load(path):
    import json
    return json.loads(path.read_text())


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

    def test_only_base_path_is_checked(self, storage_manager):
        # Exported studies now stay in base_path until purged; there is no
        # separate retained directory to fall through to.
        (storage_manager.export_path / "0002").mkdir(parents=True)
        path, location = storage_manager.resolve_study_dir(2)
        assert path is None
        assert location == "absent"


class TestFindSidecarByNumber:
    def test_delegates_to_study_manager_index(self, storage_manager):
        register_study(storage_manager, 1, "ready")
        sc = storage_manager.find_sidecar_by_number(1)
        assert sc is not None
        assert sc.original_study_uid == "study-uid-1"

    def test_unknown_number_returns_none(self, storage_manager):
        assert storage_manager.find_sidecar_by_number(999) is None


class TestListStudiesSummarySync:
    """Covers the exam_corrupt flag: a corrupt exam sidecar must render
    distinguishably from "not yet extracted" instead of both looking like
    an empty summary in the /studies table."""

    def test_study_with_no_exam_sidecar_has_no_summary_and_not_corrupt(self, storage_manager):
        register_study(storage_manager, 1, "ready")

        results = storage_manager.list_studies_summary_sync()

        assert results[0]["exam_summary"] is None
        assert results[0]["exam_corrupt"] is False

    def test_study_with_valid_exam_sidecar_is_not_corrupt(self, storage_manager):
        register_study(storage_manager, 1, "ready")
        ExamSidecar(1, {
            "study_number": 1,
            "protocol_type": "PEDIATRIC_BODY",
            "examination_group": None,
            "indication": {"region": None, "clinical_indication": None},
            "scanner": {}, "rdsr": {}, "manual": {},
        }).save(storage_manager.base_path)

        results = storage_manager.list_studies_summary_sync()

        assert results[0]["exam_corrupt"] is False
        assert results[0]["exam_summary"]["protocol_type"] == "PEDIATRIC_BODY"

    def test_study_with_corrupt_exam_sidecar_is_flagged_not_silently_empty(self, storage_manager):
        # Regression: a corrupt sidecar used to be indistinguishable from
        # "nothing extracted yet" — both rendered exam_summary: None.
        register_study(storage_manager, 1, "ready")
        exam_path = ExamSidecar.path_for(storage_manager.base_path, 1)
        exam_path.write_text("{not valid json")

        results = storage_manager.list_studies_summary_sync()

        assert results[0]["exam_summary"] is None
        assert results[0]["exam_corrupt"] is True


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


class TestEnforceRetentionPurge:
    def _manager_with_retention(self, tmp_path, retain_days=14):
        from .conftest import make_settings
        return StorageManager(make_settings(tmp_path, storage={
            "base_path": str(tmp_path / "data"),
            "temp_path": str(tmp_path / "tmp"),
            "retain_after_export_days": retain_days,
        }))

    def _exported_study(self, sm, exported_at, *, n=1):
        """An archived study still on disk: study dir + export pair +
        deface scratch, with exported_at stamped `exported_at` seconds ago."""
        sc = register_study(sm, n, "archived", archived_via="rhythm")
        write_minimal_dicom(sm.base_path / f"{n:04d}" / "0001" / "img.dcm")
        sm.export_path.mkdir(parents=True, exist_ok=True)
        (sm.export_path / f"{n:04d}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        (sm.export_path / f"{n:04d}.json").write_text("{}")
        scratch = sm.temp_path / f"pixieveil_deface_{n:04d}_0001"
        (scratch / "nifti_in").mkdir(parents=True)
        sc.exported_at = _iso_ago(exported_at)
        sc.save(sm.base_path)
        return sc

    def test_expired_study_is_purged_but_record_kept(self, tmp_path):
        sm = self._manager_with_retention(tmp_path, retain_days=1)
        self._exported_study(sm, exported_at=999999)

        sm.enforce_retention_purge_sync()

        assert not (sm.base_path / "0001").exists()
        assert not (sm.export_path / "0001.zip").exists()
        assert not (sm.export_path / "0001.json").exists()
        assert not (sm.temp_path / "pixieveil_deface_0001_0001").exists()
        # record survives, marked purged
        reloaded = StudySidecar.load(StudySidecar.path_for(sm.base_path, 1))
        assert reloaded.purged_at is not None

    def test_not_yet_expired_study_is_kept(self, tmp_path):
        sm = self._manager_with_retention(tmp_path, retain_days=14)
        self._exported_study(sm, exported_at=60)

        sm.enforce_retention_purge_sync()

        assert (sm.base_path / "0001").exists()
        assert (sm.export_path / "0001.zip").exists()

    def test_unexported_archived_study_is_not_purged(self, tmp_path):
        sm = self._manager_with_retention(tmp_path, retain_days=1)
        # archived (e.g. kept locally) but exported_at never set
        register_study(sm, 1, "archived")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img.dcm")

        sm.enforce_retention_purge_sync()

        assert (sm.base_path / "0001").exists()

    def test_retention_switched_off_purges_already_exported_studies(self, tmp_path):
        # Exported while a 14-day window was set, then retention switched off
        # ("delete immediately"): the next pass purges it, record kept.
        sm = self._manager_with_retention(tmp_path, retain_days=14)
        self._exported_study(sm, exported_at=60)
        sm.enforce_retention_purge_sync()
        assert (sm.base_path / "0001").exists()

        sm.settings.storage["retain_after_export_days"] = None
        sm.enforce_retention_purge_sync()

        assert not (sm.base_path / "0001").exists()
        assert not (sm.export_path / "0001.zip").exists()
        reloaded = StudySidecar.load(StudySidecar.path_for(sm.base_path, 1))
        assert reloaded.purged_at is not None

    def test_retention_off_still_keeps_studies_never_exported(self, tmp_path):
        # No exported_at (e.g. a RHYTHM job not yet COMPLETE) => keep.
        from .conftest import make_settings
        sm = StorageManager(make_settings(tmp_path))
        register_study(sm, 1, "archived", archived_via="dicom")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img.dcm")
        register_study(sm, 2, "ready")
        write_minimal_dicom(sm.base_path / "0002" / "0001" / "img.dcm")

        sm.enforce_retention_purge_sync()

        assert (sm.base_path / "0001").exists()
        assert (sm.base_path / "0002").exists()


class TestHttpExportArtifacts:
    def _sm(self, tmp_path, **extra_storage):
        from .conftest import make_settings
        storage = {
            "base_path": str(tmp_path / "data"),
            "temp_path": str(tmp_path / "tmp"),
            "remote_storage": {"http": {"base_url": "https://storage.example"}},
        }
        storage.update(extra_storage)
        sm = StorageManager(make_settings(tmp_path, storage=storage))

        async def _ok(zip_path, name):
            _ok.sent = (zip_path, name)
            return True
        sm.remote_storage.upload_file = _ok
        sm.remote_storage.enabled = True
        return sm

    def test_http_writes_export_pair_and_keeps_it_with_retention(self, tmp_path):
        sm = self._sm(tmp_path, retain_after_export_days=7)
        register_study(sm, 1, "ready")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img.dcm")

        r = asyncio.run(sm.manual_upload_http(1))
        assert r["ok"] is True

        assert (sm.export_path / "0001.zip").exists()
        desc = _json_load(sm.export_path / "0001.json")
        assert desc["exported_via"] == "http"
        assert desc["status"] == "sent"
        assert desc["zip"]["sha256"]
        sc = sm.find_sidecar_by_number(1)
        assert sc.status == "archived" and sc.archived_via == "http"
        assert sc.exported_at is not None and sc.purged_at is None
        assert (sm.base_path / "0001").exists()

    def test_http_no_retention_purges_pair_and_dir_immediately(self, tmp_path):
        sm = self._sm(tmp_path)
        register_study(sm, 1, "ready")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img.dcm")

        asyncio.run(sm.manual_upload_http(1))

        assert not (sm.base_path / "0001").exists()
        assert not (sm.export_path / "0001.zip").exists()
        sc = sm.find_sidecar_by_number(1)
        assert sc.exported_at is not None and sc.purged_at is not None

    def test_export_clears_deface_scratch(self, tmp_path):
        sm = self._sm(tmp_path, retain_after_export_days=7)
        register_study(sm, 1, "ready")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img.dcm")
        (sm.temp_path / "pixieveil_deface_0001_0001").mkdir(parents=True)

        asyncio.run(sm.manual_upload_http(1))

        # retention keeps the study dir but the NIfTI scratch is gone
        assert (sm.base_path / "0001").exists()
        assert not (sm.temp_path / "pixieveil_deface_0001_0001").exists()


class TestOrphanDefaceScratchSweep:
    def test_startup_sweeps_only_archived_and_absent_studies(self, tmp_path):
        from .conftest import make_settings
        settings = make_settings(tmp_path)
        # First manager instance: lay down state, then build scratch dirs.
        sm = StorageManager(settings)
        register_study(sm, 1, "archived", archived_via="dicom")
        for n, status in [(2, "ready"), (3, "defacing_failed"), (4, "receiving")]:
            register_study(sm, n, status)
            write_minimal_dicom(sm.base_path / f"{n:04d}" / "0001" / "img.dcm")
        for n in (1, 2, 3, 4, 9):
            (sm.temp_path / f"pixieveil_deface_{n:04d}_0001").mkdir(parents=True)

        # A fresh manager runs the sweep in __init__.
        sm2 = StorageManager(settings)

        assert not (sm2.temp_path / "pixieveil_deface_0001_0001").exists()  # archived
        assert not (sm2.temp_path / "pixieveil_deface_0009_0001").exists()  # no study
        # everything not archived keeps its scratch for manual inspection
        assert (sm2.temp_path / "pixieveil_deface_0002_0001").exists()      # ready
        assert (sm2.temp_path / "pixieveil_deface_0003_0001").exists()      # defacing_failed
        assert (sm2.temp_path / "pixieveil_deface_0004_0001").exists()      # receiving


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

    @pytest.mark.parametrize("age, expected", [
        (0.2, "002M"),     # neonate head group (< 0.25 y) — not "000Y"
        (0.24, "002M"),    # truncated, never rounded up across 0.25
        (0.6, "007M"),     # infant (< 1 y) — not "001Y" (would be group 3)
        (0.99, "011M"),
        (1.5, "018M"),
        (5.9, "005Y"),     # completed years: still < 6 → group 3
        (8.0, "008Y"),
        (0.01, "003D"),
        (0.0, "000D"),
    ])
    def test_age_written_in_a_unit_that_keeps_the_age_group(
            self, storage_manager, age, expected):
        from pixieveil.processing.exam_extractor import HEAD_AGE_GROUPS

        def group(a):
            return next(g for upper, g in HEAD_AGE_GROUPS if a < upper)

        study_dir, p1, _ = self._study_with_two_images(storage_manager)
        storage_manager._writeback_patient_fields_sync(study_dir, None, age)
        written = read_dicom(p1).PatientAge
        assert written == expected
        assert group(_parse_dicom_age(written)) == group(age)

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

    def test_dicom_writeback_failure_is_reported_not_silently_swallowed(self, storage_manager):
        # Regression: the sidecar write succeeding used to be reported as a
        # bare "ok" with no way to tell whether the correction actually
        # reached the DICOM files it's meant to travel with.
        dcm_path = self._setup_ready_study(storage_manager)
        dcm_path.write_bytes(b"not a dicom file")  # dcmread will raise on it

        result = asyncio.run(storage_manager.save_exam(1, {"patient.weight_kg": 25.0}))

        assert result["ok"] is True  # the sidecar itself did save
        assert result["data"]["patient"]["weight_kg"] == 25.0
        assert "1 DICOM file" in result["message"]


class TestExamHints:
    """Pre-anonymization extraction hints — captured from raw images before
    anonymize() strips Manufacturer/model/descriptions under the 'rhythm'
    profile, handed back to ExamExtractor."""

    def test_merge_mem_first_non_empty_wins_across_images_and_series(self, storage_manager):
        sm = storage_manager
        uid = "s-uid"
        # series 1, image 1: no Manufacturer
        sm._merge_exam_hints_mem(uid, 1, {"manufacturer": "", "model": "",
                                          "protocol_name": "P1",
                                          "study_description": "",
                                          "series_description": "S1"})
        # series 1, image 2: Manufacturer now present — tops up the study block
        sm._merge_exam_hints_mem(uid, 1, {"manufacturer": "SIEMENS", "model": "Emotion 16",
                                          "protocol_name": "P1-later",
                                          "study_description": "SD",
                                          "series_description": "S1-later"})
        # series 2, image 1
        sm._merge_exam_hints_mem(uid, 2, {"manufacturer": "IGNORED", "model": "",
                                          "protocol_name": "", "study_description": "",
                                          "series_description": "S2"})

        h = sm._exam_hints[uid]
        assert h["study"] == {"manufacturer": "SIEMENS", "model": "Emotion 16",
                              "protocol_name": "P1", "study_description": "SD"}
        assert h["series"] == {"1": {"series_description": "S1"},
                               "2": {"series_description": "S2"}}

    def test_flush_and_load_round_trip(self, storage_manager, tmp_path):
        study_dir = tmp_path / "0001"
        study_dir.mkdir()
        hints = {"study": {"manufacturer": "SIEMENS"}, "series": {}}
        storage_manager._flush_exam_hints_sync(study_dir, hints)
        assert (study_dir / ".exam_hints.json").exists()
        assert storage_manager._load_exam_hints_sync(study_dir) == hints

    def test_flush_noop_on_empty(self, storage_manager, tmp_path):
        study_dir = tmp_path / "0001"
        study_dir.mkdir()
        storage_manager._flush_exam_hints_sync(study_dir, None)
        storage_manager._flush_exam_hints_sync(study_dir, {})
        assert not (study_dir / ".exam_hints.json").exists()
        assert storage_manager._load_exam_hints_sync(study_dir) is None

    def test_write_exam_sidecar_uses_hints_when_headers_blank(self, storage_manager):
        sm = storage_manager
        study_dir = sm.base_path / "0001"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT", SeriesNumber=1)
        register_study(sm, 1, "complete")
        sm._exam_hints["study-uid-1"] = {
            "study": {"manufacturer": "SIEMENS", "model": "Emotion 16"},
            "series": {},
        }

        sm._write_exam_sidecar("study-uid-1", 1, study_dir)

        data = ExamSidecar.load(ExamSidecar.path_for(sm.base_path, 1)).data
        assert data["scanner"]["manufacturer"] == "SIEMENS"
        assert data["scanner"]["model"] == "Emotion 16"

    def test_reextract_reads_hints_from_disk(self, storage_manager):
        sm = storage_manager
        study_dir = sm.base_path / "0001"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT", SeriesNumber=1)
        register_study(sm, 1, "ready")
        (study_dir / ".exam_hints.json").write_text(
            '{"study": {"manufacturer": "SIEMENS", "model": "Emotion 16"}, "series": {}}'
        )

        result = asyncio.run(sm.manual_reextract(1))

        assert result["ok"] is True
        assert result["data"]["scanner"]["manufacturer"] == "SIEMENS"

    def test_write_exam_sidecar_persists_the_winning_hints_view(self, storage_manager):
        sm = storage_manager
        study_dir = sm.base_path / "0001"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", Modality="CT", SeriesNumber=1)
        register_study(sm, 1, "complete")
        sm._exam_hints["study-uid-1"] = {"study": {"manufacturer": "SIEMENS"}, "series": {}}

        sm._write_exam_sidecar("study-uid-1", 1, study_dir)

        # in-memory view flushed to disk so a later restart + re-extract sees it
        assert sm._load_exam_hints_sync(study_dir) == {
            "study": {"manufacturer": "SIEMENS"}, "series": {}
        }

    def test_purge_study_artifacts_removes_dir_export_pair_and_scratch(self, tmp_path):
        from .conftest import make_settings
        sm = StorageManager(make_settings(tmp_path, storage={
            "base_path": str(tmp_path / "data"),
            "temp_path": str(tmp_path / "tmp"),
        }))
        study_dir = sm.base_path / "0001"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm")
        (study_dir / ".exam_hints.json").write_text('{"study": {}}')
        sm.export_path.mkdir(parents=True, exist_ok=True)
        (sm.export_path / "0001.zip").write_bytes(b"z")
        (sm.export_path / "0001.json").write_text("{}")
        (sm.temp_path / "pixieveil_deface_0001_0002").mkdir(parents=True)

        sm._purge_study_artifacts_sync(1)

        assert not study_dir.exists()
        assert not (sm.export_path / "0001.zip").exists()
        assert not (sm.export_path / "0001.json").exists()
        assert not (sm.temp_path / "pixieveil_deface_0001_0002").exists()


class TestManualReextract:
    def _setup_study(self, storage_manager, study_number=1):
        study_dir = storage_manager.base_path / f"{study_number:04d}"
        write_minimal_dicom(study_dir / "0001" / "img1.dcm", SeriesNumber=1)
        register_study(storage_manager, study_number, "ready")
        return study_dir

    def test_reextract_with_no_previous_sidecar_succeeds(self, storage_manager):
        self._setup_study(storage_manager)
        result = asyncio.run(storage_manager.manual_reextract(1))
        assert result["ok"] is True

    def test_reextract_preserves_manual_overlay_from_valid_previous_sidecar(self, storage_manager):
        self._setup_study(storage_manager)
        ExamSidecar(1, {
            "study_number": 1,
            "patient": {"weight_kg": None, "age_years": None},
            "indication": {"region": None, "clinical_indication": None},
            "manual": {"edited_at": "t", "fields": {"patient.weight_kg": 12.0}},
            "notes": [],
        }).save(storage_manager.base_path)

        result = asyncio.run(storage_manager.manual_reextract(1))

        assert result["ok"] is True
        assert result["data"]["patient"]["weight_kg"] == 12.0

    def test_reextract_with_corrupt_previous_sidecar_fails_loudly(self, storage_manager):
        # Regression: this used to log a warning and silently proceed
        # "without" the corrupt sidecar's manual overlay — discarding any
        # hand-entered corrections it held while still reporting ok:True.
        self._setup_study(storage_manager)
        exam_path = ExamSidecar.path_for(storage_manager.base_path, 1)
        exam_path.write_text("{not valid json")

        result = asyncio.run(storage_manager.manual_reextract(1))

        assert result["ok"] is False
        assert result["message"] == "re-extraction failed"


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


class TestManualExportBlockedOnDefacingFailure:
    def _manager_with_dicom_configured(self, tmp_path):
        from .conftest import make_settings
        return StorageManager(make_settings(tmp_path, storage={
            "base_path": str(tmp_path / "data"),
            "temp_path": str(tmp_path / "tmp"),
            "remote_storage": {
                "dicom": {"host": "127.0.0.1", "port": 4070, "ae_title": "REMOTE"},
                "http": {"base_url": "https://storage.example"},
            },
        }))

    def test_send_dicom_refuses_a_defacing_failed_study(self, tmp_path):
        sm = self._manager_with_dicom_configured(tmp_path)
        register_study(sm, 1, "defacing_failed")
        (sm.base_path / "0001").mkdir(parents=True)

        result = asyncio.run(sm.manual_send_dicom(1))

        assert result["ok"] is False
        assert "defacing did not complete" in result["message"]

    def test_upload_http_refuses_a_defacing_failed_study(self, tmp_path):
        sm = self._manager_with_dicom_configured(tmp_path)
        register_study(sm, 1, "defacing_failed")
        (sm.base_path / "0001").mkdir(parents=True)

        result = asyncio.run(sm.manual_upload_http(1))

        assert result["ok"] is False
        assert "defacing did not complete" in result["message"]

    def test_ready_study_is_not_blocked_by_the_defacing_failed_guard(self, tmp_path, monkeypatch):
        # Sanity check the guard is specific to defacing_failed, not a
        # regression that blocks every export. Uses the same fake AE as
        # test_dicom_storage.py so this never opens a real association.
        from .test_dicom_storage import FakeAssociation, install_fake_ae

        sm = self._manager_with_dicom_configured(tmp_path)
        register_study(sm, 1, "ready")
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img1.dcm")
        install_fake_ae(monkeypatch, FakeAssociation())

        result = asyncio.run(sm.manual_send_dicom(1))

        assert "defacing did not complete" not in result.get("message", "")
        assert result["ok"] is True


class TestDefacingDisabled:
    """With defacing off, head-scan series are still classified so the study
    lands in defacing_disabled (export blocked) instead of silently ready."""

    def _study_with_series(self, sm, description):
        sc = register_study(sm, 1, "complete")
        sc.add_series("series-uid-1", 1, "anon-series-1")
        sc.save(sm.base_path)
        sm.study_manager._base_path = sm.base_path
        sm.study_manager._active_waiting = 0
        write_minimal_dicom(sm.base_path / "0001" / "0001" / "img1.dcm",
                            SeriesDescription=description)
        sm._active_waiting = 1
        return sc

    def test_head_study_is_marked_defacing_disabled(self, storage_manager):
        assert storage_manager.defacer.enabled is False
        self._study_with_series(storage_manager, "Head routine")

        asyncio.run(storage_manager._process_study("study-uid-1"))

        sc = storage_manager.study_manager.get_sidecar_by_number(1)
        assert sc.status == "defacing_disabled"
        rec = sc.series["series-uid-1"]
        assert rec.is_head is True and rec.defaced is False

    def test_non_head_study_is_ready(self, storage_manager):
        self._study_with_series(storage_manager, "Abdomen")

        asyncio.run(storage_manager._process_study("study-uid-1"))

        assert storage_manager.study_manager.get_sidecar_by_number(1).status == "ready"

    def test_head_study_with_defacing_enabled_but_failing_is_defacing_failed(
            self, storage_manager, monkeypatch):
        self._study_with_series(storage_manager, "Head routine")
        storage_manager.defacer.enabled = True
        monkeypatch.setattr(storage_manager.defacer, "deface_series",
                            lambda *a, **k: False)

        asyncio.run(storage_manager._process_study("study-uid-1"))

        assert storage_manager.study_manager.get_sidecar_by_number(1).status == "defacing_failed"

    @pytest.mark.parametrize("enabled, deface_ok, expected", [
        (True, False, "defacing_failed"),
        (False, None, "defacing_disabled"),
        (True, True, "ready"),
    ])
    def test_head_series_without_a_resolvable_uid_still_blocks_export(
            self, storage_manager, monkeypatch, enabled, deface_ok, expected):
        # The series folder exists but its original UID can't be looked up
        # (no sidecar record): its head/defaced state can't be recorded, so
        # it must fail closed instead of letting the study reach "ready".
        sc = register_study(storage_manager, 1, "complete")
        sc.save(storage_manager.base_path)
        storage_manager.study_manager._base_path = storage_manager.base_path
        storage_manager._active_waiting = 1
        write_minimal_dicom(storage_manager.base_path / "0001" / "0001" / "img1.dcm",
                            SeriesDescription="Head routine")
        storage_manager.defacer.enabled = enabled
        monkeypatch.setattr(storage_manager.defacer, "deface_series",
                            lambda *a, **k: deface_ok)

        asyncio.run(storage_manager._process_study("study-uid-1"))

        assert storage_manager.study_manager.get_sidecar_by_number(1).status == expected

    def test_exports_refuse_a_defacing_disabled_study(self, tmp_path):
        sm = TestManualExportBlockedOnDefacingFailure()._manager_with_dicom_configured(tmp_path)
        register_study(sm, 1, "defacing_disabled")
        (sm.base_path / "0001").mkdir(parents=True)

        for action in (sm.manual_send_dicom, sm.manual_upload_http):
            result = asyncio.run(action(1))
            assert result["ok"] is False
            assert "defacing is disabled" in result["message"]


class TestStatusCountsAndExamSummary:
    def test_status_counts_report_every_status(self, storage_manager):
        register_study(storage_manager, 1, "ready")
        register_study(storage_manager, 2, "archived", archived_via="dicom")
        register_study(storage_manager, 3, "ready")

        counts = storage_manager.get_status_counts_sync()

        assert counts == {"receiving": 0, "complete": 0, "defacing": 0,
                          "defacing_failed": 0, "defacing_disabled": 0,
                          "ready": 2, "archived": 1}

    def test_exam_summary_reports_missing_fields_and_completeness(self):
        summary = StorageManager._exam_summary({
            "image_quality": None,
            "patient": {"age_years": 5.0, "weight_kg": None},
            "indication": {"region": "Chest", "clinical_indication": "Fungal infections",
                           "iv_contrast": "Non-contrast"},
            "scanner": {"rhythm_scanner_id": "uuid"},
            "rhythm": {"protocol_key": "head_trauma"},
        })
        assert summary["missing_fields"] == ["image_quality", "patient.weight_kg"]
        assert summary["completeness"] == {"filled": 5, "required": 7}
        assert summary["patient"] == {"age_years": 5.0, "weight_kg": None}


class RecordingBus:
    """Stands in for EventBus: records publish/progress calls, no loop."""

    def __init__(self):
        self.events: list[tuple] = []

    def publish(self, type_, **data):
        self.events.append((type_, data))

    def progress(self, n, kind, done, total=None, label=None, *, force=False):
        self.events.append(("progress", {"n": n, "kind": kind, "done": done, "total": total}))

    def counters(self):
        pass

    def of(self, type_):
        return [d for t, d in self.events if t == type_]


class TestLiveEvents:
    def test_status_transitions_publish_study_events(self, storage_manager):
        bus = storage_manager.events = RecordingBus()
        register_study(storage_manager, 1, "complete")
        storage_manager.study_manager._base_path = storage_manager.base_path

        storage_manager.study_manager.mark_study_ready("study-uid-1")
        storage_manager.study_manager.mark_study_archived("study-uid-1", via="dicom")

        assert bus.of("study") == [{"n": 1, "status": "ready"}, {"n": 1, "status": "archived"}]

    def test_defacing_publishes_per_series_progress(self, storage_manager, monkeypatch):
        bus = storage_manager.events = RecordingBus()
        sc = register_study(storage_manager, 1, "complete")
        for i in (1, 2):
            sc.add_series(f"series-uid-{i}", i, f"anon-{i}")
            write_minimal_dicom(storage_manager.base_path / "0001" / f"{i:04d}" / "img.dcm",
                                SeriesDescription="Head routine")
        storage_manager.study_manager._base_path = storage_manager.base_path
        storage_manager.defacer.enabled = True
        monkeypatch.setattr(storage_manager.defacer, "deface_series", lambda *a, **k: True)

        storage_manager._deface_study("study-uid-1", 1, storage_manager.base_path / "0001")

        prog = [(p["done"], p["total"]) for p in bus.of("progress") if p["kind"] == "deface"]
        assert prog == [(0, 2), (1, 2), (2, 2)]


class TestCompletionLoop:
    """completion_loop: every step runs each tick, in order; one step
    raising never starves the others; stop_event ends it promptly."""

    STEPS = ("check_study_completions", "enforce_storage_quota",
             "enforce_retention_purge", "reconcile_rhythm_jobs",
             "reapply_catalog_if_changed", "prepare_ready_exports")

    def _run(self, tmp_path, monkeypatch, *, failing=(), ticks=2, interval=0.01):
        from .conftest import make_settings
        sm = StorageManager(make_settings(
            tmp_path, study={"completion_check_interval": interval}))
        calls: list[str] = []

        def fake(name):
            async def step(*args):
                calls.append(name)
                if calls.count(self.STEPS[-1]) >= ticks:
                    sm._stop_event.set()
                if name in failing:
                    raise RuntimeError(f"{name} boom")
            return step

        for name in self.STEPS:
            monkeypatch.setattr(sm, name, fake(name))

        async def main():
            sm._stop_event = asyncio.Event()
            await asyncio.wait_for(sm.completion_loop(), timeout=5)
        asyncio.run(main())
        return calls

    def test_every_step_runs_each_tick_in_order(self, tmp_path, monkeypatch):
        calls = self._run(tmp_path, monkeypatch, ticks=2)
        assert calls == list(self.STEPS) * 2

    def test_a_failing_step_does_not_starve_the_others(self, tmp_path, monkeypatch, caplog):
        calls = self._run(tmp_path, monkeypatch, ticks=2,
                          failing=set(self.STEPS[:-1]))
        assert calls == list(self.STEPS) * 2
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 2 * (len(self.STEPS) - 1)

    def test_last_step_failing_still_loops(self, tmp_path, monkeypatch):
        calls = self._run(tmp_path, monkeypatch, ticks=3, failing={self.STEPS[-1]})
        assert calls == list(self.STEPS) * 3

    def test_stop_during_wait_exits_without_waiting_the_interval(self, tmp_path, monkeypatch):
        # Interval of an hour: the loop must still exit right after the tick
        # that set the stop event (wait_for returns on the event, not timeout).
        import time as _time
        t0 = _time.monotonic()
        calls = self._run(tmp_path, monkeypatch, ticks=1, interval=3600)
        assert calls == list(self.STEPS)
        assert _time.monotonic() - t0 < 2

    def test_already_stopped_runs_no_tick(self, tmp_path, monkeypatch):
        from .conftest import make_settings
        sm = StorageManager(make_settings(tmp_path))
        calls = []

        async def step(*a):
            calls.append(1)
        for name in self.STEPS:
            monkeypatch.setattr(sm, name, step)

        async def main():
            sm._stop_event = asyncio.Event()
            sm._stop_event.set()
            await sm.completion_loop()
        asyncio.run(main())
        assert calls == []


class TestProcessImageTempCleanup:
    """process_image's failure/skip paths must not leave the raw, still
    identifiable received image in temp_path: nothing else ever deletes or
    reprocesses it, so a leftover is a permanent PHI copy."""

    def _raw(self, sm, **attrs):
        from pydicom.uid import generate_uid
        sm.temp_path.mkdir(parents=True, exist_ok=True)
        p = sm.temp_path / "img-1.dcm"
        base = dict(StudyInstanceUID=generate_uid(), SeriesInstanceUID=generate_uid(),
                    PatientID="REAL-PATIENT-42", PatientName="Doe^Jane")
        base.update(attrs)
        write_minimal_dicom(p, **{k: v for k, v in base.items() if v is not None})
        return p

    def test_success_moves_file_out_of_temp(self, storage_manager):
        p = self._raw(storage_manager)
        storage_manager.process_image(p, "img-1")
        assert not p.exists()
        assert list(storage_manager.base_path.rglob("0001.dcm"))

    def test_invalid_dicom_is_deleted(self, storage_manager):
        p = self._raw(storage_manager, SeriesInstanceUID=None)
        storage_manager.process_image(p, "img-1")
        assert not p.exists()
        assert storage_manager.counters["processing"]["errors"]["validation"] == 1

    def test_anonymization_failure_is_deleted(self, storage_manager, monkeypatch):
        p = self._raw(storage_manager)

        def boom(ds):
            raise RuntimeError("anonymizer broke")
        monkeypatch.setattr(storage_manager.anonymizer, "anonymize", boom)
        storage_manager.process_image(p, "img-1")
        assert not p.exists()
        assert storage_manager.counters["processing"]["errors"]["anonymization"] == 1
        assert not list(storage_manager.base_path.rglob("*.dcm"))

    def test_unexpected_error_after_anonymization_is_deleted(self, storage_manager,
                                                             monkeypatch):
        p = self._raw(storage_manager)

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(storage_manager.study_manager, "add_image_to_study", boom)
        storage_manager.process_image(p, "img-1")
        assert not p.exists()
        assert storage_manager.counters["processing"]["errors"]["processing"] == 1

    def test_unreadable_file_is_deleted(self, storage_manager):
        storage_manager.temp_path.mkdir(parents=True, exist_ok=True)
        p = storage_manager.temp_path / "img-1.dcm"
        p.write_bytes(b"\0" * 10)
        storage_manager.process_image(p, "img-1")
        assert not p.exists()

    def test_skipped_during_shutdown_is_deleted(self, storage_manager):
        p = self._raw(storage_manager)
        storage_manager._shutting_down = True
        storage_manager.process_image(p, "img-1")
        assert not p.exists()

    def test_study_size_limit_drops_image(self, tmp_path):
        from .conftest import make_settings
        sm = StorageManager(make_settings(tmp_path, study={"max_study_size_mb": 0.000001}))
        p = self._raw(sm)
        sm.process_image(p, "img-1")
        assert not p.exists()
        assert sm.counters["processing"]["filtered_images"] == 1
        assert not list(sm.base_path.rglob("*.dcm"))

    def test_new_study_counts_in_storage_studies(self, storage_manager):
        p = self._raw(storage_manager)
        storage_manager.process_image(p, "img-1")
        assert storage_manager.get_counter("storage", "studies") == 1
        assert storage_manager.get_counter("storage", "series") == 1


class TestAuditLogInjection:
    def test_detail_with_newline_cannot_forge_an_audit_line(self, storage_manager, caplog):
        import logging
        register_study(storage_manager, 1, "ready")
        evil = "boom\nAudit: user=admin action=mark_exported study=0001 ok=True"
        with caplog.at_level(logging.INFO, logger="pixieveil.storage.storage_manager"):
            storage_manager.record_audit(1, "alice", "upload_rhythm", False, evil)
        lines = [r.getMessage() for r in caplog.records if "Audit:" in r.getMessage()]
        assert len(lines) == 1
        assert "\n" not in lines[0] and "\r" not in lines[0]
        assert lines[0].startswith("Audit: user=alice action=upload_rhythm")
        assert "detail='boom\\nAudit:" in lines[0]    # inert, inside the quoted detail
        # the sidecar's structured audit keeps the original text
        sc = storage_manager.find_sidecar_by_number(1)
        assert sc.audit[-1]["detail"] == evil
