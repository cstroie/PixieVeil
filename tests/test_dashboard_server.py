"""Unit tests for Dashboard's data-loading helpers (not the HTTP routes —
those need a running aiohttp app, out of scope for this pass). Currently
covers _load_studies_sync's exam_corrupt flag, added so a corrupt exam
sidecar renders distinguishably from "not yet extracted" instead of both
looking like an empty "—" in the /studies table."""

from pixieveil.config import Settings
from pixieveil.dashboard.server import Dashboard
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.storage_manager import StorageManager

from .conftest import make_settings
from .test_storage_manager import register_study


def make_dashboard(storage_manager: StorageManager) -> Dashboard:
    return Dashboard(Settings(), storage_manager)


class TestLoadStudiesSync:
    def test_study_with_no_exam_sidecar_has_no_summary_and_not_corrupt(self, tmp_path):
        sm = StorageManager(make_settings(tmp_path))
        register_study(sm, 1, "ready")
        dashboard = make_dashboard(sm)

        results = dashboard._load_studies_sync(sm)

        assert results[0]["exam_summary"] is None
        assert results[0]["exam_corrupt"] is False

    def test_study_with_valid_exam_sidecar_is_not_corrupt(self, tmp_path):
        sm = StorageManager(make_settings(tmp_path))
        register_study(sm, 1, "ready")
        ExamSidecar(1, {
            "study_number": 1,
            "protocol_type": "PEDIATRIC_BODY",
            "examination_group": None,
            "indication": {"region": None, "clinical_indication": None},
            "scanner": {}, "rdsr": {}, "manual": {},
        }).save(sm.base_path)
        dashboard = make_dashboard(sm)

        results = dashboard._load_studies_sync(sm)

        assert results[0]["exam_corrupt"] is False
        assert results[0]["exam_summary"]["protocol_type"] == "PEDIATRIC_BODY"

    def test_study_with_corrupt_exam_sidecar_is_flagged_not_silently_empty(self, tmp_path):
        # Regression: a corrupt sidecar used to be indistinguishable from
        # "nothing extracted yet" — both rendered exam_summary: None.
        sm = StorageManager(make_settings(tmp_path))
        register_study(sm, 1, "ready")
        exam_path = ExamSidecar.path_for(sm.base_path, 1)
        exam_path.write_text("{not valid json")
        dashboard = make_dashboard(sm)

        results = dashboard._load_studies_sync(sm)

        assert results[0]["exam_summary"] is None
        assert results[0]["exam_corrupt"] is True
