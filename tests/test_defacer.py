"""Unit tests for Defacer.

Deliberately out of scope (per CLAUDE.md's Testing section): dicom_to_nifti,
the main nifti_to_dicom pixel-array conversion, run_nnunet_inference,
_run_nnunet_and_apply_mask, and _dilate_mask. Those need SimpleITK/nibabel/
nnU-Net and real volumetric image data to exercise meaningfully — testing
them with synthetic 2x2 arrays would verify the test's own fixture, not the
conversion math. What's covered here instead:

- Config parsing (__init__)
- _resolve_device's cuda/mps/cpu branches, via monkeypatching torch instead
  of depending on what GPU (if any) actually runs this test
- is_head_scan / is_topogram (pure pydicom header logic)
- _ensure_model (pure filesystem path resolution)
- _prepare_for_write (pure pydicom dataset mutation)
- _load_series_groups (pure pydicom directory grouping/sorting)
- deface_series's orchestration and atomic-swap safety: the three heavy
  conversion steps (dicom_to_nifti, _run_defacing_tool, nifti_to_dicom) are
  monkeypatched to instance-level stubs, so what's actually under test is
  the directory bookkeeping around them — which step's failure leaves the
  original series untouched, whether keep_backup does the right thing, and
  that a failed atomic swap restores the backup rather than losing data.
"""

import sys
from pathlib import Path

import pydicom
import pytest
from pydicom.tag import Tag

from pixieveil.processing.defacer import Defacer

from .conftest import write_minimal_dicom


def make_defacer(tmp_path, **cfg) -> Defacer:
    cfg.setdefault("enabled", True)
    return Defacer(cfg, temp_path=tmp_path / "deface_tmp")


class TestConfig:
    def test_defaults(self, tmp_path):
        d = Defacer(None, temp_path=tmp_path)
        assert d.enabled is False
        assert d.keep_backup is True
        assert d.rotation_mode == "iop"
        assert d.mask_dilation_mm == 2.0
        assert d.model_dir is None
        assert d._body_parts == {"HEAD", "BRAIN", "NECK", "SKULL"}

    def test_body_parts_uppercased(self, tmp_path):
        d = Defacer({"body_parts": ["head", "Neck"]}, temp_path=tmp_path)
        assert d._body_parts == {"HEAD", "NECK"}

    def test_custom_description_pattern_compiled(self, tmp_path):
        d = Defacer({"series_description_pattern": r"custom-pattern"}, temp_path=tmp_path)
        assert d._desc_re.search("this has custom-pattern in it")
        assert not d._desc_re.search("nothing relevant here")

    def test_model_dir_resolved_from_config(self, tmp_path):
        d = Defacer({"model_dir": str(tmp_path / "models")}, temp_path=tmp_path)
        assert d.model_dir == tmp_path / "models"


class TestResolveDevice:
    """Monkeypatches torch so these are deterministic regardless of what
    GPU (if any) is actually available in the environment running the
    test suite."""

    def test_torch_not_installed_returns_requested_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", None)
        assert Defacer._resolve_device("cuda") == "cuda"

    def test_cuda_available_and_functional(self, tmp_path, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        monkeypatch.setattr(torch, "zeros", lambda *a, **k: None)
        assert Defacer._resolve_device("cuda") == "cuda"

    def test_cuda_allocation_failure_falls_back_to_cpu(self, tmp_path, monkeypatch):
        import torch

        def raise_alloc(*a, **k):
            raise RuntimeError("out of memory")

        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        monkeypatch.setattr(torch, "zeros", raise_alloc)
        assert Defacer._resolve_device("cuda") == "cpu"

    def test_cuda_unavailable_falls_back_to_cpu(self, tmp_path, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        assert Defacer._resolve_device("cuda") == "cpu"

    def test_mps_available(self, tmp_path, monkeypatch):
        import torch
        if not hasattr(torch.backends, "mps"):
            pytest.skip("this torch build has no mps backend module")
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
        assert Defacer._resolve_device("mps") == "mps"

    def test_mps_unavailable_falls_back_to_cpu(self, tmp_path, monkeypatch):
        import torch
        if not hasattr(torch.backends, "mps"):
            pytest.skip("this torch build has no mps backend module")
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
        assert Defacer._resolve_device("mps") == "cpu"

    def test_explicit_cpu_stays_cpu(self, tmp_path):
        assert Defacer._resolve_device("cpu") == "cpu"


class TestIsHeadScan:
    def test_matches_body_part_examined(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        write_minimal_dicom(series_dir / "img1.dcm", BodyPartExamined="HEAD")
        assert d.is_head_scan(series_dir) is True

    def test_matches_series_description_pattern(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        write_minimal_dicom(series_dir / "img1.dcm", SeriesDescription="Brain Routine")
        assert d.is_head_scan(series_dir) is True

    def test_no_match_returns_false(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        write_minimal_dicom(series_dir / "img1.dcm",
                             BodyPartExamined="ABDOMEN", SeriesDescription="Routine Abdomen")
        assert d.is_head_scan(series_dir) is False

    def test_empty_dir_returns_false(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        series_dir.mkdir(parents=True)
        assert d.is_head_scan(series_dir) is False

    def test_non_dicom_file_sorted_first_defeats_detection(self, tmp_path):
        # Documents a real limitation, not desired behavior: dcmread's
        # force=True doesn't raise on garbage bytes — it fabricates a bogus
        # dataset from misread bytes (confirmed: 1 nonsense element, not an
        # empty one) — so the except-and-continue skip never fires, and
        # is_head_scan's "only inspect one representative file" design
        # stops there without ever reaching the real DICOM file that
        # follows. Low real-world risk since PixieVeil fully controls what
        # lands in its own series directories, but worth having a test
        # that fails loudly if this ever gets "fixed" with a fragile
        # heuristic that changes the behavior unintentionally.
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        series_dir.mkdir(parents=True)
        (series_dir / "0_bad.dcm").write_bytes(b"not a dicom file")
        write_minimal_dicom(series_dir / "1_good.dcm", BodyPartExamined="HEAD")
        assert d.is_head_scan(series_dir) is False

    def test_non_file_directory_entry_is_skipped(self, tmp_path):
        # A stray subdirectory (not a stray file) is the one thing that's
        # filtered before dcmread ever runs (`if not f.is_file(): continue`)
        # — this exercises that guard, letting the scan reach the real
        # DICOM file that follows it.
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        series_dir.mkdir(parents=True)
        (series_dir / "0_dir.dcm").mkdir()
        write_minimal_dicom(series_dir / "1_good.dcm", BodyPartExamined="HEAD")
        assert d.is_head_scan(series_dir) is True


class TestIsTopogram:
    def test_matches_localizer_image_type(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        write_minimal_dicom(series_dir / "img1.dcm", ImageType=["ORIGINAL", "LOCALIZER"])
        assert d.is_topogram(series_dir) is True

    def test_matches_series_description(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        write_minimal_dicom(series_dir / "img1.dcm", SeriesDescription="Scout view")
        assert d.is_topogram(series_dir) is True

    def test_matches_scan_options(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        write_minimal_dicom(series_dir / "img1.dcm", ScanOptions="TOPOGRAM")
        assert d.is_topogram(series_dir) is True

    def test_regular_axial_series_is_not_a_topogram(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = tmp_path / "0001" / "0001"
        write_minimal_dicom(series_dir / "img1.dcm",
                             ImageType=["ORIGINAL", "PRIMARY", "AXIAL"],
                             SeriesDescription="Axial 5mm")
        assert d.is_topogram(series_dir) is False


class TestEnsureModel:
    def test_configured_model_dir_with_dataset_present(self, tmp_path):
        model_root = tmp_path / "models"
        (model_root / Defacer._MODEL_DATASET).mkdir(parents=True)
        d = make_defacer(tmp_path, model_dir=str(model_root))
        assert d._ensure_model() == model_root

    def test_configured_model_dir_missing_dataset_raises(self, tmp_path):
        model_root = tmp_path / "models"
        model_root.mkdir()
        d = make_defacer(tmp_path, model_dir=str(model_root))
        with pytest.raises(RuntimeError, match=Defacer._MODEL_DATASET):
            d._ensure_model()

    def test_data_dir_fallback_when_model_dir_not_configured(self, tmp_path):
        data_dir = tmp_path / "data" / "dicom"
        expected_root = tmp_path / "data" / "nnUNet"
        (expected_root / Defacer._MODEL_DATASET).mkdir(parents=True)
        d = make_defacer(tmp_path)
        assert d._ensure_model(data_dir) == expected_root

    def test_neither_model_dir_nor_data_dir_raises(self, tmp_path):
        d = make_defacer(tmp_path)
        with pytest.raises(RuntimeError, match="model_dir"):
            d._ensure_model()


class TestPrepareForWrite:
    def test_pixel_data_vr_ow_for_16_bit(self):
        ds = pydicom.Dataset()
        ds.BitsAllocated = 16
        ds.add_new(Tag(0x7FE0, 0x0010), "OW", b"\x00\x00")
        Defacer._prepare_for_write(ds)
        assert ds["PixelData"].VR == "OW"

    def test_pixel_data_vr_ob_for_8_bit(self):
        ds = pydicom.Dataset()
        ds.BitsAllocated = 8
        ds.add_new(Tag(0x7FE0, 0x0010), "OW", b"\x00")
        Defacer._prepare_for_write(ds)
        assert ds["PixelData"].VR == "OB"

    def test_missing_file_meta_is_created(self):
        ds = pydicom.Dataset()
        Defacer._prepare_for_write(ds)
        assert ds.file_meta is not None

    def test_missing_transfer_syntax_gets_implicit_vr_little_endian(self):
        from pydicom.uid import ImplicitVRLittleEndian
        ds = pydicom.Dataset()
        Defacer._prepare_for_write(ds)
        assert ds.file_meta.TransferSyntaxUID == ImplicitVRLittleEndian

    def test_existing_transfer_syntax_is_left_alone(self):
        from pydicom.dataset import FileMetaDataset
        from pydicom.uid import ExplicitVRLittleEndian
        ds = pydicom.Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        Defacer._prepare_for_write(ds)
        assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian


class TestLoadSeriesGroups:
    def test_groups_by_series_instance_uid(self, tmp_path):
        d = make_defacer(tmp_path)
        write_minimal_dicom(tmp_path / "a.dcm", SeriesInstanceUID="1.1")
        write_minimal_dicom(tmp_path / "b.dcm", SeriesInstanceUID="1.1")
        write_minimal_dicom(tmp_path / "c.dcm", SeriesInstanceUID="2.2")

        groups = d._load_series_groups(tmp_path)

        assert set(groups.keys()) == {"1.1", "2.2"}
        assert len(groups["1.1"]) == 2
        assert len(groups["2.2"]) == 1

    def test_sorted_by_instance_number(self, tmp_path):
        d = make_defacer(tmp_path)
        write_minimal_dicom(tmp_path / "a.dcm", SeriesInstanceUID="1.1", InstanceNumber=3)
        write_minimal_dicom(tmp_path / "b.dcm", SeriesInstanceUID="1.1", InstanceNumber=1)
        write_minimal_dicom(tmp_path / "c.dcm", SeriesInstanceUID="1.1", InstanceNumber=2)

        groups = d._load_series_groups(tmp_path)
        ordered_paths = [p for p, _ds in groups["1.1"]]
        assert [Path(p).name for p in ordered_paths] == ["b.dcm", "c.dcm", "a.dcm"]

    def test_falls_back_to_image_position_patient_z(self, tmp_path):
        d = make_defacer(tmp_path)
        write_minimal_dicom(tmp_path / "a.dcm", SeriesInstanceUID="1.1",
                             ImagePositionPatient=[0.0, 0.0, 30.0])
        write_minimal_dicom(tmp_path / "b.dcm", SeriesInstanceUID="1.1",
                             ImagePositionPatient=[0.0, 0.0, -10.0])

        groups = d._load_series_groups(tmp_path)
        ordered_paths = [Path(p).name for p, _ds in groups["1.1"]]
        assert ordered_paths == ["b.dcm", "a.dcm"]

    def test_no_readable_dicom_raises(self, tmp_path):
        d = make_defacer(tmp_path)
        (tmp_path / "bad.dcm").write_bytes(b"not a dicom file")
        with pytest.raises(ValueError, match="No readable DICOM series"):
            d._load_series_groups(tmp_path)

    def test_corrupt_file_skipped_others_grouped(self, tmp_path):
        d = make_defacer(tmp_path)
        write_minimal_dicom(tmp_path / "good.dcm", SeriesInstanceUID="1.1")
        (tmp_path / "bad.dcm").write_bytes(b"not a dicom file")
        groups = d._load_series_groups(tmp_path)
        assert len(groups["1.1"]) == 1


class TestDefaceSeriesOrchestration:
    """The three heavy conversion steps are stubbed at the instance level
    (assigning a plain function to an instance attribute bypasses bound-
    method lookup, so `self.dicom_to_nifti(a, b)` just calls the stub with
    those args) — this tests deface_series's directory bookkeeping and
    atomic-swap safety, not image conversion."""

    def _series_with_one_file(self, tmp_path, content=b"ORIGINAL"):
        series_dir = tmp_path / "0001" / "0001"
        series_dir.mkdir(parents=True)
        (series_dir / "img1.dcm").write_bytes(content)
        return series_dir

    def _stub_success(self, d: Defacer, output_marker: bytes = b"DEFACED"):
        """Wire dicom_to_nifti/_run_defacing_tool/nifti_to_dicom so the
        pipeline "succeeds" and nifti_to_dicom leaves a marker file in its
        output_dir, standing in for real defaced DICOM output."""
        d.dicom_to_nifti = lambda dicom_dir, output_dir, series_instance_uid=None: (
            str(Path(output_dir) / "fake.nii.gz")
        )
        d._run_defacing_tool = lambda nifti_path, nifti_in_dir, nifti_out_dir, data_dir=None: (
            Path(nifti_out_dir) / "fake_defaced.nii.gz"
        )

        def fake_nifti_to_dicom(nifti_file, dicom_template_dir, output_dir, rotation_mode="iop"):
            out = Path(output_dir) / "out.dcm"
            out.write_bytes(output_marker)
            return [str(out)]

        d.nifti_to_dicom = fake_nifti_to_dicom

    def test_disabled_returns_false_and_touches_nothing(self, tmp_path):
        d = make_defacer(tmp_path, enabled=False)
        series_dir = self._series_with_one_file(tmp_path)
        original_content = (series_dir / "img1.dcm").read_bytes()

        result = d.deface_series(series_dir)

        assert result is False
        assert (series_dir / "img1.dcm").read_bytes() == original_content

    def test_dicom_to_nifti_failure_leaves_series_intact(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = self._series_with_one_file(tmp_path)

        def raiser(*a, **k):
            raise RuntimeError("conversion failed")

        d.dicom_to_nifti = raiser

        result = d.deface_series(series_dir)

        assert result is False
        assert series_dir.exists()
        assert (series_dir / "img1.dcm").read_bytes() == b"ORIGINAL"
        assert not series_dir.parent.joinpath("0001_pre_deface").exists()

    def test_defacing_tool_failure_leaves_series_intact(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = self._series_with_one_file(tmp_path)
        self._stub_success(d)

        def raiser(*a, **k):
            raise RuntimeError("nnunet failed")

        d._run_defacing_tool = raiser

        result = d.deface_series(series_dir)

        assert result is False
        assert (series_dir / "img1.dcm").read_bytes() == b"ORIGINAL"

    def test_nifti_to_dicom_failure_leaves_series_intact(self, tmp_path):
        d = make_defacer(tmp_path)
        series_dir = self._series_with_one_file(tmp_path)
        self._stub_success(d)

        def raiser(*a, **k):
            raise RuntimeError("reconstruction failed")

        d.nifti_to_dicom = raiser

        result = d.deface_series(series_dir)

        assert result is False
        assert (series_dir / "img1.dcm").read_bytes() == b"ORIGINAL"

    def test_successful_defacing_keeps_backup_by_default(self, tmp_path):
        d = make_defacer(tmp_path, keep_backup=True)
        series_dir = self._series_with_one_file(tmp_path)
        self._stub_success(d)

        result = d.deface_series(series_dir)

        assert result is True
        assert (series_dir / "out.dcm").read_bytes() == b"DEFACED"
        backup_dir = series_dir.parent / "0001_pre_deface"
        assert backup_dir.exists()
        assert (backup_dir / "img1.dcm").read_bytes() == b"ORIGINAL"
        assert not (series_dir.parent / "0001_staged").exists()

    def test_successful_defacing_without_backup(self, tmp_path):
        d = make_defacer(tmp_path, keep_backup=False)
        series_dir = self._series_with_one_file(tmp_path)
        self._stub_success(d)

        result = d.deface_series(series_dir)

        assert result is True
        assert (series_dir / "out.dcm").read_bytes() == b"DEFACED"
        assert not (series_dir.parent / "0001_pre_deface").exists()

    def test_staging_failure_returns_false_and_cleans_up(self, tmp_path, monkeypatch):
        import pixieveil.processing.defacer as defacer_module

        d = make_defacer(tmp_path)
        series_dir = self._series_with_one_file(tmp_path)
        self._stub_success(d)

        def raise_copytree(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(defacer_module.shutil, "copytree", raise_copytree)

        result = d.deface_series(series_dir)

        assert result is False
        assert (series_dir / "img1.dcm").read_bytes() == b"ORIGINAL"
        assert not (series_dir.parent / "0001_staged").exists()

    def test_swap_failure_restores_backup(self, tmp_path, monkeypatch):
        import pathlib

        d = make_defacer(tmp_path, keep_backup=True)
        series_dir = self._series_with_one_file(tmp_path)
        self._stub_success(d)

        original_rename = pathlib.Path.rename

        def flaky_rename(self, target):
            # Only the second rename in the swap (staged_dir -> series_dir)
            # fails, so the first (series_dir -> backup_dir) has already
            # succeeded by the time this fires — exercising the "restore
            # from backup" recovery path deliberately, not by accident.
            if self.name.endswith("_staged"):
                raise OSError("simulated rename failure")
            return original_rename(self, target)

        monkeypatch.setattr(pathlib.Path, "rename", flaky_rename)

        result = d.deface_series(series_dir)

        assert result is False
        # The original series must survive under its real name — not lost
        # mid-swap and not left stuck under the backup name.
        assert series_dir.exists()
        assert (series_dir / "img1.dcm").read_bytes() == b"ORIGINAL"
        assert not (series_dir.parent / "0001_pre_deface").exists()
        assert not (series_dir.parent / "0001_staged").exists()
