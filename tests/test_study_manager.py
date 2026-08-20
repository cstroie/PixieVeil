"""Unit tests for StudyManager: numbering, completion detection, the
number_map reverse index, and sidecar-backed crash recovery."""

import time

from pixieveil.config import Settings
from pixieveil.processing.study_manager import StudyManager
from pixieveil.storage.study_sidecar import StudySidecar


def make_study_manager(tmp_path, completion_timeout=120) -> StudyManager:
    sm = StudyManager(Settings(study={"completion_timeout": completion_timeout}))
    sm.initialize_from_sidecars(tmp_path)
    return sm


class TestAddImageToStudy:
    def test_first_image_assigns_study_and_series_1(self, tmp_path):
        sm = make_study_manager(tmp_path)
        study_num, series_num, image_num, is_new_series = sm.add_image_to_study(
            "study-uid-1", "series-uid-1"
        )
        assert study_num == 1
        assert series_num == 1
        assert image_num == 1
        assert is_new_series is True

    def test_second_image_same_series_increments_image_number(self, tmp_path):
        sm = make_study_manager(tmp_path)
        sm.add_image_to_study("study-uid-1", "series-uid-1")
        _, series_num, image_num, is_new_series = sm.add_image_to_study(
            "study-uid-1", "series-uid-1"
        )
        assert series_num == 1
        assert image_num == 2
        assert is_new_series is False

    def test_new_series_same_study_increments_series_number(self, tmp_path):
        sm = make_study_manager(tmp_path)
        sm.add_image_to_study("study-uid-1", "series-uid-1")
        _, series_num, _, is_new_series = sm.add_image_to_study("study-uid-1", "series-uid-2")
        assert series_num == 2
        assert is_new_series is True

    def test_second_study_gets_next_study_number(self, tmp_path):
        sm = make_study_manager(tmp_path)
        sm.add_image_to_study("study-uid-1", "series-uid-1")
        study_num, series_num, _, _ = sm.add_image_to_study("study-uid-2", "series-uid-1")
        assert study_num == 2
        assert series_num == 1  # series numbering restarts per study


class TestNumberMapIndex:
    def test_new_study_populates_number_map(self, tmp_path):
        sm = make_study_manager(tmp_path)
        sm.add_image_to_study("study-uid-1", "series-uid-1")
        assert sm.number_map[1] == "study-uid-1"

    def test_get_sidecar_by_number_after_record_new_series(self, tmp_path):
        sm = make_study_manager(tmp_path)
        study_num, series_num, _, _ = sm.add_image_to_study("study-uid-1", "series-uid-1")
        sm.record_new_series(
            "study-uid-1", "series-uid-1", "orig-pid",
            "anon-study-uid-1", "anon-series-uid-1", "anon-pid",
            study_num, series_num,
        )
        sc = sm.get_sidecar_by_number(study_num)
        assert sc is not None
        assert sc.original_study_uid == "study-uid-1"

    def test_get_sidecar_by_number_unknown_number_returns_none(self, tmp_path):
        sm = make_study_manager(tmp_path)
        assert sm.get_sidecar_by_number(999) is None

    def test_get_sidecar_by_uid_after_record_new_series(self, tmp_path):
        sm = make_study_manager(tmp_path)
        study_num, series_num, _, _ = sm.add_image_to_study("study-uid-1", "series-uid-1")
        sm.record_new_series(
            "study-uid-1", "series-uid-1", "orig-pid",
            "anon-study-uid-1", "anon-series-uid-1", "anon-pid",
            study_num, series_num,
        )
        sc = sm.get_sidecar_by_uid("study-uid-1")
        assert sc is not None
        assert sc.study_number == study_num

    def test_get_sidecar_by_uid_unknown_uid_returns_none(self, tmp_path):
        sm = make_study_manager(tmp_path)
        assert sm.get_sidecar_by_uid("no-such-uid") is None

    def test_has_undefaced_head_series_unknown_uid_returns_false(self, tmp_path):
        sm = make_study_manager(tmp_path)
        assert sm.has_undefaced_head_series("no-such-uid") is False

    def test_has_undefaced_head_series_delegates_to_sidecar(self, tmp_path):
        sm = make_study_manager(tmp_path)
        study_num, series_num, _, _ = sm.add_image_to_study("study-uid-1", "series-uid-1")
        sm.record_new_series(
            "study-uid-1", "series-uid-1", "orig-pid",
            "anon-study-uid-1", "anon-series-uid-1", "anon-pid",
            study_num, series_num,
        )
        assert sm.has_undefaced_head_series("study-uid-1") is False
        sm.set_series_classification("study-uid-1", "series-uid-1", is_head=True, is_topogram=False)
        assert sm.has_undefaced_head_series("study-uid-1") is True
        sm.mark_series_defaced("study-uid-1", "series-uid-1")
        assert sm.has_undefaced_head_series("study-uid-1") is False

    def test_number_map_restored_on_initialize_from_sidecars(self, tmp_path):
        sc = StudySidecar.create(
            study_number=5,
            original_study_uid="restored-uid",
            original_patient_id="pid",
            anonymized_study_uid="anon-uid",
            anonymized_patient_id="anon-pid",
        )
        sc.status = "archived"
        sc.save(tmp_path)

        sm = StudyManager(Settings())
        sm.initialize_from_sidecars(tmp_path)
        assert sm.get_sidecar_by_number(5).original_study_uid == "restored-uid"


class TestCheckStudyCompletions:
    def test_study_not_yet_timed_out_is_not_completed(self, tmp_path):
        sm = make_study_manager(tmp_path, completion_timeout=3600)
        sm.add_image_to_study("study-uid-1", "series-uid-1")
        assert sm.check_study_completions() == []

    def test_timed_out_study_is_returned_once(self, tmp_path):
        sm = make_study_manager(tmp_path, completion_timeout=120)
        sm.add_image_to_study("study-uid-1", "series-uid-1")
        # Force the timeout deterministically instead of sleeping.
        sm.study_states["study-uid-1"].last_received = time.time() - 999999
        assert sm.check_study_completions() == ["study-uid-1"]
        # A second call must not re-report the same study.
        assert sm.check_study_completions() == []

    def test_timed_out_study_sidecar_status_becomes_complete(self, tmp_path):
        sm = make_study_manager(tmp_path, completion_timeout=120)
        study_num, series_num, _, _ = sm.add_image_to_study("study-uid-1", "series-uid-1")
        sm.record_new_series(
            "study-uid-1", "series-uid-1", "orig-pid",
            "anon-study-uid-1", "anon-series-uid-1", "anon-pid",
            study_num, series_num,
        )
        sm.study_states["study-uid-1"].last_received = time.time() - 999999
        sm.check_study_completions()
        assert sm.get_sidecar_by_number(study_num).status == "complete"


class TestLifecycleTransitions:
    def _sidecar_study(self, sm, tmp_path):
        study_num, series_num, _, _ = sm.add_image_to_study("study-uid-1", "series-uid-1")
        sm.record_new_series(
            "study-uid-1", "series-uid-1", "orig-pid",
            "anon-study-uid-1", "anon-series-uid-1", "anon-pid",
            study_num, series_num,
        )
        return study_num

    def test_mark_study_ready_sets_status_and_clears_active_state(self, tmp_path):
        sm = make_study_manager(tmp_path)
        self._sidecar_study(sm, tmp_path)
        sm.mark_study_ready("study-uid-1")
        assert sm.get_sidecar_by_number(1).status == "ready"
        assert "study-uid-1" not in sm.study_states

    def test_mark_study_archived_sets_status_and_via(self, tmp_path):
        sm = make_study_manager(tmp_path)
        self._sidecar_study(sm, tmp_path)
        sm.mark_study_archived("study-uid-1", via="dicom")
        sc = sm.get_sidecar_by_number(1)
        assert sc.status == "archived"
        assert sc.archived_via == "dicom"
        assert "study-uid-1" not in sm.study_states

    def test_mark_study_defacing_failed_sets_status_and_clears_active_state(self, tmp_path):
        sm = make_study_manager(tmp_path)
        self._sidecar_study(sm, tmp_path)
        sm.mark_study_defacing_failed("study-uid-1")
        assert sm.get_sidecar_by_number(1).status == "defacing_failed"
        assert "study-uid-1" not in sm.study_states


class TestRecoveryRequeue:
    def _save(self, tmp_path, study_number, status, with_dir=True):
        sc = StudySidecar.create(
            study_number=study_number,
            original_study_uid=f"uid-{study_number}",
            original_patient_id="pid",
            anonymized_study_uid="anon-uid",
            anonymized_patient_id="anon-pid",
        )
        sc.status = status
        sc.save(tmp_path)
        if with_dir:
            (tmp_path / f"{study_number:04d}").mkdir(parents=True, exist_ok=True)
        return sc

    def test_complete_and_defacing_studies_are_requeued(self, tmp_path):
        self._save(tmp_path, 1, "complete")
        self._save(tmp_path, 2, "defacing")

        sm = StudyManager(Settings())
        sm.initialize_from_sidecars(tmp_path)
        assert set(sm._recovered_studies) == {"uid-1", "uid-2"}

    def test_defacing_failed_study_is_requeued_to_retry(self, tmp_path):
        # A restart should retry the series that never got defaced —
        # is_series_defaced already makes re-running defacing idempotent
        # for series that succeeded the first time.
        self._save(tmp_path, 5, "defacing_failed")

        sm = StudyManager(Settings())
        sm.initialize_from_sidecars(tmp_path)
        assert sm._recovered_studies == ["uid-5"]

    def test_ready_and_archived_studies_are_not_requeued(self, tmp_path):
        self._save(tmp_path, 3, "ready")
        self._save(tmp_path, 4, "archived")

        sm = StudyManager(Settings())
        sm.initialize_from_sidecars(tmp_path)
        assert sm._recovered_studies == []

    def test_complete_study_missing_its_directory_is_not_requeued(self, tmp_path):
        # A crash could leave a sidecar behind after the study directory was
        # already cleaned up elsewhere — nothing to re-process in that case.
        self._save(tmp_path, 6, "complete", with_dir=False)

        sm = StudyManager(Settings())
        sm.initialize_from_sidecars(tmp_path)
        assert sm._recovered_studies == []
