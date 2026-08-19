"""Unit tests for ZipManager: pure filesystem/zip logic, no network or GPU
involved — real tmp_path directories and zipfile throughout."""

import asyncio
import zipfile

from pixieveil.config import Settings
from pixieveil.storage.zip_manager import ZipManager


def make_manager(base_path) -> ZipManager:
    return ZipManager(Settings(storage={"base_path": str(base_path)}))


class TestCreateZipSync:
    def test_includes_files_recursively_with_relative_arcnames(self, tmp_path):
        base_path = tmp_path / "data"
        study_dir = base_path / "0001"
        (study_dir / "0001").mkdir(parents=True)
        (study_dir / "0001" / "img1.dcm").write_bytes(b"img1")
        (study_dir / "0002").mkdir(parents=True)
        (study_dir / "0002" / "img2.dcm").write_bytes(b"img2")

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        manager = make_manager(base_path)

        zip_path = manager.create_zip_sync("0001", output_dir)

        assert zip_path == output_dir / "0001.zip"
        with zipfile.ZipFile(zip_path) as zf:
            names = sorted(zf.namelist())
            assert names == ["0001/img1.dcm", "0002/img2.dcm"]
            assert zf.read("0001/img1.dcm") == b"img1"
            assert zf.read("0002/img2.dcm") == b"img2"

    def test_default_source_dir_is_base_path_slash_study_uid(self, tmp_path):
        base_path = tmp_path / "data"
        (base_path / "0007" / "0001").mkdir(parents=True)
        (base_path / "0007" / "0001" / "img.dcm").write_bytes(b"x")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        manager = make_manager(base_path)

        zip_path = manager.create_zip_sync("0007", output_dir)

        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == ["0001/img.dcm"]

    def test_explicit_source_dir_overrides_default(self, tmp_path):
        base_path = tmp_path / "data"  # deliberately has nothing under it
        retained_dir = tmp_path / "retained" / "0001"
        (retained_dir / "0001").mkdir(parents=True)
        (retained_dir / "0001" / "img.dcm").write_bytes(b"retained-content")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        manager = make_manager(base_path)

        zip_path = manager.create_zip_sync("0001", output_dir, source_dir=retained_dir)

        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == ["0001/img.dcm"]
            assert zf.read("0001/img.dcm") == b"retained-content"

    def test_pre_deface_backup_dirs_excluded(self, tmp_path):
        base_path = tmp_path / "data"
        study_dir = base_path / "0001"
        (study_dir / "0001").mkdir(parents=True)
        (study_dir / "0001" / "img.dcm").write_bytes(b"current")
        (study_dir / "0001_pre_deface").mkdir(parents=True)
        (study_dir / "0001_pre_deface" / "img.dcm").write_bytes(b"backup")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        manager = make_manager(base_path)

        zip_path = manager.create_zip_sync("0001", output_dir)

        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == ["0001/img.dcm"]
            assert zf.read("0001/img.dcm") == b"current"

    def test_empty_study_dir_produces_empty_zip(self, tmp_path):
        base_path = tmp_path / "data"
        (base_path / "0001").mkdir(parents=True)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        manager = make_manager(base_path)

        zip_path = manager.create_zip_sync("0001", output_dir)

        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == []

    def test_nonexistent_source_dir_produces_empty_zip_not_an_error(self, tmp_path):
        # rglob() on a directory that doesn't exist yields nothing rather
        # than raising, so this doesn't hit the except branch in create_zip
        # — it "succeeds" with a zero-entry archive. Worth having a test
        # that pins this down explicitly rather than relying on nobody
        # ever calling create_zip for an already-vanished study.
        base_path = tmp_path / "data"  # not created
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        manager = make_manager(base_path)

        zip_path = manager.create_zip_sync("0001", output_dir)

        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == []


class TestCreateZipAsync:
    def test_delegates_to_sync_version(self, tmp_path):
        base_path = tmp_path / "data"
        (base_path / "0001").mkdir(parents=True)
        (base_path / "0001" / "img.dcm").write_bytes(b"x")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        manager = make_manager(base_path)

        zip_path = asyncio.run(manager.create_zip("0001", output_dir))

        assert zip_path == output_dir / "0001.zip"
        assert zip_path.exists()

    def test_returns_none_when_output_dir_does_not_exist(self, tmp_path):
        # zipfile.ZipFile(path, "w") raises FileNotFoundError when the
        # parent directory doesn't exist — create_zip must catch that and
        # return None rather than propagating.
        base_path = tmp_path / "data"
        (base_path / "0001").mkdir(parents=True)
        (base_path / "0001" / "img.dcm").write_bytes(b"x")
        manager = make_manager(base_path)

        zip_path = asyncio.run(manager.create_zip("0001", tmp_path / "does_not_exist"))

        assert zip_path is None
