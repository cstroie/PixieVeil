"""Unit tests for StudySidecar's lifecycle helpers and atomic JSON I/O."""

from pixieveil.storage.study_sidecar import StudySidecar


def _make(study_number=7):
    return StudySidecar.create(
        study_number=study_number,
        original_study_uid="1.2.3.orig",
        original_patient_id="ORIGPID",
        anonymized_study_uid="1.2.3.anon",
        anonymized_patient_id="ANONPID",
    )


class TestCreate:
    def test_starts_in_receiving_status(self):
        sc = _make()
        assert sc.status == "receiving"
        assert sc.archived_via is None
        assert sc.series == {}


class TestSeriesLifecycle:
    def test_add_series_is_idempotent(self):
        sc = _make()
        sc.add_series("orig-series-1", series_number=1, anonymized_series_uid="anon-series-1")
        sc.add_series("orig-series-1", series_number=1, anonymized_series_uid="anon-series-1")
        assert len(sc.series) == 1

    def test_mark_series_defaced(self):
        sc = _make()
        sc.add_series("orig-series-1", series_number=1, anonymized_series_uid="anon-series-1")
        assert sc.is_series_defaced("orig-series-1") is False
        sc.mark_series_defaced("orig-series-1")
        assert sc.is_series_defaced("orig-series-1") is True

    def test_mark_series_defaced_unknown_series_is_a_noop(self):
        sc = _make()
        sc.mark_series_defaced("does-not-exist")  # must not raise

    def test_get_series_uid_for_number(self):
        sc = _make()
        sc.add_series("orig-series-1", series_number=3, anonymized_series_uid="anon-series-1")
        assert sc.get_series_uid_for_number(3) == "orig-series-1"
        assert sc.get_series_uid_for_number(99) is None

    def test_set_series_classification(self):
        sc = _make()
        sc.add_series("orig-series-1", series_number=1, anonymized_series_uid="anon-series-1")
        sc.set_series_classification("orig-series-1", is_head=True, is_topogram=False)
        rec = sc.series["orig-series-1"]
        assert rec.is_head is True
        assert rec.is_topogram is False


class TestPersistence:
    def test_save_then_load_round_trips(self, tmp_path):
        sc = _make(study_number=42)
        sc.add_series("orig-series-1", series_number=1, anonymized_series_uid="anon-series-1")
        sc.mark_series_defaced("orig-series-1")
        sc.status = "archived"
        sc.archived_via = "dicom"
        sc.save(tmp_path)

        loaded = StudySidecar.load(StudySidecar.path_for(tmp_path, 42))
        assert loaded.study_number == 42
        assert loaded.status == "archived"
        assert loaded.archived_via == "dicom"
        assert loaded.series["orig-series-1"].defaced is True

    def test_save_is_atomic_no_leftover_tmp_file(self, tmp_path):
        sc = _make(study_number=5)
        sc.save(tmp_path)
        assert (tmp_path / "0005.json").exists()
        assert not (tmp_path / "0005.tmp").exists()

    def test_load_all_keys_by_original_study_uid(self, tmp_path):
        _make(study_number=1).save(tmp_path)
        sc2 = _make(study_number=2)
        sc2.original_study_uid = "1.2.3.orig.other"
        sc2.save(tmp_path)

        sidecars = StudySidecar.load_all(tmp_path)
        assert set(sidecars.keys()) == {"1.2.3.orig", "1.2.3.orig.other"}
        assert sidecars["1.2.3.orig.other"].study_number == 2

    def test_load_all_skips_corrupt_file(self, tmp_path):
        _make(study_number=1).save(tmp_path)
        (tmp_path / "0002.json").write_text("{not valid json")

        sidecars = StudySidecar.load_all(tmp_path)
        assert len(sidecars) == 1
