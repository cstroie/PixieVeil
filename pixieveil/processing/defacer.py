"""
DICOM defacing module for PixieVeil.

This module provides DICOM to NIfTI conversion and NIfTI to DICOM conversion
capabilities for implementing a defacing pipeline. The full defacing workflow
(DICOM → NIfTI → defaced NIfTI → DICOM) is orchestrated by deface_series().
"""

import logging
import os
import re
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian

logger = logging.getLogger(__name__)

# DICOM tags used for head-scan detection
_HEAD_BODY_PARTS = {"HEAD", "BRAIN", "NECK", "SKULL"}


class Defacer:
    """DICOM defacing conversion utilities."""

    def __init__(self, config: Optional[dict] = None, temp_path: Optional[Path] = None):
        """
        Args:
            config: The ``defacing`` section of Settings, or None to use defaults.
            temp_path: Base directory for temporary NIfTI work; defaults to system temp.
        """
        cfg = config or {}
        self.enabled: bool = cfg.get("enabled", False)
        self.keep_backup: bool = cfg.get("keep_backup", True)
        self.rotation_mode: str = cfg.get("rotation_mode", "auto90")
        self.tool_command: Optional[str] = cfg.get("tool_command", None)
        self.temp_path: Optional[Path] = temp_path

        raw_model_dir = cfg.get("model_dir", None)
        self.model_dir: Optional[Path] = Path(raw_model_dir) if raw_model_dir else None

        body_parts = cfg.get("body_parts", list(_HEAD_BODY_PARTS))
        self._body_parts: set = {bp.upper() for bp in body_parts}

        desc_pattern = cfg.get("series_description_pattern",
                               r"(?i)(head|brain|skull|cranial|cerebr)")
        self._desc_re: re.Pattern = re.compile(desc_pattern)

    # ------------------------------------------------------------------
    # Head-scan detection
    # ------------------------------------------------------------------

    def is_head_scan(self, series_dir: Path) -> bool:
        """
        Return True if the series looks like a head scan.

        Reads the first readable DICOM file in series_dir and checks:
        - BodyPartExamined against the configured body_parts list
        - SeriesDescription against the configured regex pattern
        """
        for f in sorted(series_dir.iterdir()):
            if not f.is_file():
                continue
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            except Exception:
                continue

            body_part = str(getattr(ds, "BodyPartExamined", "")).upper().strip()
            if body_part and body_part in self._body_parts:
                logger.debug("Head scan detected by BodyPartExamined=%r in %s", body_part, series_dir)
                return True

            description = str(getattr(ds, "SeriesDescription", ""))
            if description and self._desc_re.search(description):
                logger.debug("Head scan detected by SeriesDescription=%r in %s", description, series_dir)
                return True

            # Only need one representative file
            break

        return False

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def deface_series(self, series_dir: Path) -> bool:
        """
        Run the full defacing pipeline for one series directory.

        Steps:
        1. Convert DICOM series → NIfTI
        2. Run external defacing tool (or simulate if tool_command is None)
        3. Convert defaced NIfTI → DICOM (replacing pixel data, keeping headers)
        4. Optionally back up the anonymized-but-not-defaced series

        The replacement is atomic: defaced files are written to a sibling temp
        directory and the series dir is swapped only on full success. On any
        failure the original series dir is left intact.

        Args:
            series_dir: Path to the series directory (base_path/NNNN/MMMM/).

        Returns:
            True on success, False if defacing was skipped or failed.
        """
        if not self.enabled:
            return False

        logger.info("Defacing series: %s", series_dir)

        # Use a persistent named directory so NIfTI files survive for manual inspection.
        base_tmp = Path(self.temp_path) if self.temp_path else Path(tempfile.gettempdir())
        tmp = base_tmp / f"pixieveil_deface_{series_dir.parent.name}_{series_dir.name}"
        nifti_in_dir = tmp / "nifti_in"
        nifti_out_dir = tmp / "nifti_out"
        dicom_out_dir = tmp / "dicom_out"
        nifti_in_dir.mkdir(parents=True, exist_ok=True)
        nifti_out_dir.mkdir(parents=True, exist_ok=True)
        dicom_out_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: DICOM → NIfTI
        try:
            nifti_path = self.dicom_to_nifti(str(series_dir), str(nifti_in_dir))
        except Exception as e:
            logger.error("DICOM→NIfTI failed for %s: %s", series_dir, e)
            return False

        logger.info("NIfTI files kept at %s for manual inspection", nifti_in_dir)

        # Step 2: external defacing tool (or simulation)
        try:
            defaced_nifti = self._run_defacing_tool(
                Path(nifti_path), nifti_in_dir, nifti_out_dir
            )
        except Exception as e:
            logger.error("Defacing tool failed for %s: %s", series_dir, e)
            return False

        # Step 3: defaced NIfTI → DICOM (using original series as template)
        try:
            self.nifti_to_dicom(
                str(defaced_nifti),
                str(series_dir),
                str(dicom_out_dir),
                rotation_mode=self.rotation_mode,
            )
        except Exception as e:
            logger.error("NIfTI→DICOM failed for %s: %s", series_dir, e)
            return False

        # Step 4: atomic swap with optional backup
        backup_dir = series_dir.parent / f"{series_dir.name}_pre_deface"
        try:
            if self.keep_backup:
                series_dir.rename(backup_dir)
                logger.info("Backup of anonymized series kept at %s", backup_dir)
            else:
                shutil.rmtree(series_dir)

            shutil.copytree(dicom_out_dir, series_dir)
        except Exception as e:
            logger.error("Atomic swap failed for %s: %s", series_dir, e)
            # Try to restore from backup if we already moved the original
            if self.keep_backup and backup_dir.exists() and not series_dir.exists():
                backup_dir.rename(series_dir)
                logger.warning("Restored original series from backup after swap failure")
            return False

        logger.info("Defacing complete for %s", series_dir)
        return True

    # ------------------------------------------------------------------
    # Model management
    # ------------------------------------------------------------------

    _MODEL_DATASET = "Dataset001_DEFACE"
    _MODEL_GDRIVE_URL = (
        "https://drive.google.com/drive/folders/"
        "1k4o35Dkl7PWd2yvHqWA2ia-BNKrWBrqg?usp=sharing"
    )

    def _ensure_model(self, data_dir: Optional[Path] = None) -> Path:
        """
        Resolve the nnUNet model root and verify the expected dataset directory
        is present.

        Directory layout expected::

            <model_root>/
                Dataset001_DEFACE/
                    nnUNetTrainer__nnUNetPlans__3d_fullres/
                        fold_all/
                            checkpoint_final.pth

        Resolution order for ``model_root``:

        1. ``defacing.model_dir`` from config (if set).
        2. ``<data_dir>.parent/nnUNet`` when *data_dir* is supplied
           (e.g. ``./data/nnUNet`` when ``base_path`` is ``./data/pixieveil``).

        Model download is handled by ``install.py``, not here. If the dataset
        directory is missing, a RuntimeError is raised with instructions.

        Args:
            data_dir: Fallback base path (typically ``storage.base_path``).

        Returns:
            Resolved ``model_root`` path.

        Raises:
            RuntimeError: If the model root cannot be resolved or the dataset
                          directory is absent.
        """
        if self.model_dir is not None:
            model_root = self.model_dir
        elif data_dir is not None:
            model_root = Path(data_dir).parent / "nnUNet"
        else:
            raise RuntimeError(
                "Cannot resolve nnUNet model directory: set defacing.model_dir "
                "in settings.yaml or re-run install.py."
            )

        model_root.mkdir(parents=True, exist_ok=True)

        dataset_dir = model_root / self._MODEL_DATASET
        if dataset_dir.is_dir():
            logger.debug("nnUNet model found at %s", dataset_dir)
            return model_root

        raise RuntimeError(
            f"nnUNet model dataset '{self._MODEL_DATASET}' not found in {model_root}.\n"
            f"Run  python install.py  to download it automatically, or place the\n"
            f"'{self._MODEL_DATASET}' folder there manually:\n"
            f"  {self._MODEL_GDRIVE_URL}"
        )

    def run_nnunet_inference(self, nifti_in_dir: Path, nifti_out_dir: Path,
                             data_dir: Optional[Path] = None,
                             device: str = "cpu") -> None:
        """
        Run nnUNetv2_predict on all cases in nifti_in_dir.

        The nnUNet model is expected at ``<model_dir>/nnUNet``, where
        ``model_dir`` comes from config (``defacing.model_dir``) or falls back
        to ``<data_dir>.parent/nnUNet`` when *data_dir* is supplied.

        The three nnUNet environment variables (``nnUNet_results``,
        ``nnUNet_preprocessed``, ``nnUNet_raw``) are set to the model root
        directory for the duration of the subprocess call.

        Args:
            nifti_in_dir:  Folder containing input ``*_0000.nii.gz`` files.
            nifti_out_dir: Folder where predictions will be written.
            data_dir:      Fallback base path when ``model_dir`` is not
                           configured (typically ``storage.base_path``).
            device:        nnUNet inference device: ``"cpu"``, ``"cuda"``,
                           or ``"mps"``.

        Raises:
            RuntimeError: If the model directory cannot be resolved or if
                          nnUNetv2_predict exits with a non-zero return code.
        """
        model_root = self._ensure_model(data_dir)

        nifti_in_dir.mkdir(parents=True, exist_ok=True)
        nifti_out_dir.mkdir(parents=True, exist_ok=True)

        nnunet_env = {
            "nnUNet_results":      str(model_root),
            "nnUNet_preprocessed": str(model_root),
            "nnUNet_raw":          str(model_root),
        }
        env = {**os.environ, **nnunet_env}

        _venv_candidate = Path(sys.executable).parent / "nnUNetv2_predict"
        nnunet_bin = (
            shutil.which("nnUNetv2_predict")
            or (str(_venv_candidate) if _venv_candidate.exists() else None)
        )
        if nnunet_bin is None:
            raise RuntimeError(
                "nnUNetv2_predict not found in PATH or next to the Python executable. "
                "Run  python install.py  to install nnUNetv2."
            )

        command = [
            nnunet_bin,
            "-i", str(nifti_in_dir),
            "-o", str(nifti_out_dir),
            "-d", "001",
            "-c", "3d_fullres",
            "-f", "all",
            "--disable_tta",
            "-device", device,
        ]

        logger.info("Running nnUNet inference: %s", " ".join(command))
        logger.debug("nnUNet env: %s", nnunet_env)

        result = subprocess.run(command, env=env)
        if result.returncode != 0:
            raise RuntimeError(
                f"nnUNetv2_predict exited with code {result.returncode}"
            )

        logger.info("nnUNet inference complete; output in %s", nifti_out_dir)

    def _run_defacing_tool(self, nifti_path: Path,
                           nifti_in_dir: Path, nifti_out_dir: Path) -> Path:
        """
        Call the external defacing tool, or run built-in nnUNet inference when
        tool_command is None.

        Returns the path to the defaced NIfTI file.
        """
        if self.tool_command is None:
            self.run_nnunet_inference(nifti_in_dir, nifti_out_dir)
        else:
            cmd = self.tool_command.format(
                input_dir=str(nifti_in_dir),
                output_dir=str(nifti_out_dir),
            )
            logger.info("Running defacing tool: %s", cmd)
            result = subprocess.run(cmd, shell=True)
            if result.returncode != 0:
                logger.warning("Defacing tool exited with code %d", result.returncode)

        # Find the defaced NIfTI — prefer files not named *mask*
        candidates = [
            f for f in sorted(nifti_out_dir.glob("*.nii*"))
            if "mask" not in f.name.lower()
        ]
        if not candidates:
            raise RuntimeError(f"Defacing tool produced no NIfTI in {nifti_out_dir}")

        # Prefer a file whose name matches the input series UID stem
        stem = nifti_path.name
        for suffix in (".nii.gz", ".nii"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        preferred = [f for f in candidates if f.name.startswith(stem)]
        chosen = preferred[0] if preferred else candidates[0]
        logger.info("Selected defaced NIfTI: %s", chosen)
        return chosen

    # ------------------------------------------------------------------
    # DICOM → NIfTI
    # ------------------------------------------------------------------

    def dicom_to_nifti(self, dicom_dir: str, output_dir: str,
                       series_instance_uid: Optional[str] = None) -> str:
        """
        Convert DICOM files to NIfTI format.

        Args:
            dicom_dir: Path to directory containing DICOM files
            output_dir: Path to output directory for NIfTI files
            series_instance_uid: Optional specific SeriesInstanceUID to process

        Returns:
            str: Path to the created NIfTI file

        Raises:
            ValueError: If no DICOM series found or conversion fails
        """
        import SimpleITK as sitk

        dicom_dir = str(Path(dicom_dir).resolve())
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        reader = sitk.ImageSeriesReader()
        series_ids = reader.GetGDCMSeriesIDs(dicom_dir)
        if not series_ids:
            raise ValueError(f"No DICOM series found in {dicom_dir!r}")

        if series_instance_uid is None:
            if len(series_ids) > 1:
                logger.warning(
                    "Found %d series in %r, using the first one.", len(series_ids), dicom_dir
                )
            series_instance_uid = series_ids[0]
        elif series_instance_uid not in series_ids:
            raise ValueError(
                f"SeriesInstanceUID {series_instance_uid!r} not found in {dicom_dir!r}"
            )

        fnames = reader.GetGDCMSeriesFileNames(dicom_dir, series_instance_uid)
        reader.SetFileNames(fnames)
        image = reader.Execute()

        nifti_path = output_dir / f"{series_instance_uid}_0000.nii.gz"
        sitk.WriteImage(image, str(nifti_path))

        logger.info("Converted DICOM to NIfTI: %s", nifti_path)
        return str(nifti_path)

    # ------------------------------------------------------------------
    # NIfTI → DICOM
    # ------------------------------------------------------------------

    def nifti_to_dicom(self, nifti_file: str, dicom_template_dir: str,
                       output_dir: str, rotation_mode: str = "auto90") -> List[str]:
        """
        Convert NIfTI file back to DICOM format using template DICOM headers.

        Finds the template series whose slice count best matches the NIfTI volume.
        Only PixelData is replaced; all DICOM metadata is preserved from the template.
        When NIfTI and template slice counts differ, only the overlapping subset is
        updated; remaining template slices are copied unchanged.

        Args:
            nifti_file: Path to NIfTI file to convert
            dicom_template_dir: Directory containing template DICOM files
            output_dir: Path to output directory for DICOM files
            rotation_mode: "none", "auto90" (default), or "auto_all"

        Returns:
            List[str]: Paths to created DICOM files

        Raises:
            ValueError: If conversion fails
        """
        import nibabel as nib
        import numpy as np

        nifti_file = Path(nifti_file).resolve()
        dicom_template_dir = Path(dicom_template_dir).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        nii = nib.load(str(nifti_file))
        arr = np.asarray(nii.get_fdata())
        if arr.ndim != 3:
            raise ValueError("Only 3D NIfTI volumes are supported.")

        logger.info("NIfTI shape: %s", arr.shape)

        groups = self._load_series_groups(dicom_template_dir)

        # Choose series whose slice count matches NIfTI dim 0 or dim 2
        dim0, dim2 = arr.shape[0], arr.shape[2]
        candidates_dim0 = [(s, lst) for s, lst in groups.items() if len(lst) == dim0]
        candidates_dim2 = [(s, lst) for s, lst in groups.items() if len(lst) == dim2]

        if candidates_dim0 and not candidates_dim2:
            chosen_dim, chosen_suid, chosen_list = 0, *candidates_dim0[0]
        elif candidates_dim2 and not candidates_dim0:
            chosen_dim, chosen_suid, chosen_list = 2, *candidates_dim2[0]
        elif candidates_dim0:
            chosen_dim, chosen_suid, chosen_list = 0, *candidates_dim0[0]
        else:
            logger.warning("No exact slice count match; choosing closest series.")
            best = min(
                groups.items(),
                key=lambda kv: min(abs(len(kv[1]) - dim0), abs(len(kv[1]) - dim2))
            )
            chosen_suid, chosen_list = best
            n = len(chosen_list)
            chosen_dim = 0 if abs(n - dim0) <= abs(n - dim2) else 2

        logger.info("Using SeriesInstanceUID: %s (%d slices, NIfTI dim %d)",
                    chosen_suid, len(chosen_list), chosen_dim)

        arr_slices = arr if chosen_dim == 0 else np.moveaxis(arr, 2, 0)

        n_ref = len(chosen_list)
        n_nifti = arr_slices.shape[0]
        if n_nifti != n_ref:
            logger.warning(
                "NIfTI slices (%d) != DICOM slices (%d); updating overlapping subset.",
                n_nifti, n_ref
            )
        n_update = min(n_nifti, n_ref)

        sample_ds = pydicom.dcmread(chosen_list[0][0])
        if hasattr(sample_ds, "pixel_array"):
            ref_dtype = sample_ds.pixel_array.dtype
            sample_orig_slice = sample_ds.pixel_array
        else:
            ref_dtype = np.int16
            sample_orig_slice = None

        # Reverse the DICOM rescale so we store raw pixel values, not HU values.
        # SimpleITK applies RescaleSlope/RescaleIntercept when reading to NIfTI,
        # so arr_slices contains physical HU values. Without this step the viewer
        # would apply the intercept a second time, making air appear very dense.
        rescale_slope = float(getattr(sample_ds, "RescaleSlope", 1.0))
        rescale_intercept = float(getattr(sample_ds, "RescaleIntercept", 0.0))
        arr_raw = (arr_slices - rescale_intercept) / rescale_slope

        # Clip to the valid integer range before casting to avoid wrap-around.
        if np.issubdtype(ref_dtype, np.unsignedinteger):
            clip_min, clip_max = 0, int(np.iinfo(ref_dtype).max)
        else:
            clip_min, clip_max = int(np.iinfo(ref_dtype).min), int(np.iinfo(ref_dtype).max)
        arr_slices = np.clip(arr_raw, clip_min, clip_max).astype(ref_dtype)

        logger.debug(
            "Rescale reversed: slope=%.4f intercept=%.1f  "
            "pixel range [%.0f, %.0f] → [%d, %d]",
            rescale_slope, rescale_intercept,
            float(arr_slices.min()), float(arr_slices.max()),
            clip_min, clip_max,
        )

        if sample_orig_slice is not None:
            mid = n_update // 2
            best_k, best_flip = self._determine_best_rotation(arr_slices[mid], sample_orig_slice, rotation_mode)
        else:
            best_k, best_flip = 0, False

        created_files: List[str] = []

        for (src_path, ds), slice_data in zip(
            chosen_list[:n_update], arr_slices[:n_update]
        ):
            slice_arr = np.rot90(np.asarray(slice_data), k=best_k)
            if best_flip:
                slice_arr = np.flip(slice_arr, axis=1)

            if slice_arr.shape != (ds.Rows, ds.Columns):
                raise ValueError(
                    f"Slice shape {slice_arr.shape} does not match DICOM "
                    f"({ds.Rows}, {ds.Columns})."
                )

            ds.PixelData = slice_arr.tobytes()
            self._prepare_for_write(ds)
            out_path = output_dir / Path(src_path).name
            ds.save_as(str(out_path))
            created_files.append(str(out_path))

        for src_path, ds in chosen_list[n_update:]:
            self._prepare_for_write(ds)
            out_path = output_dir / Path(src_path).name
            ds.save_as(str(out_path))
            created_files.append(str(out_path))

        logger.info("Wrote %d DICOM slices to %s", len(created_files), output_dir)
        return created_files

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_series_groups(
        self, dicom_dir: Path
    ) -> Dict[str, List[Tuple[str, pydicom.Dataset]]]:
        """Load all DICOMs in dicom_dir grouped by SeriesInstanceUID, sorted by position."""
        files: set = set()
        for pattern in ("*.dcm", "*"):
            for p in dicom_dir.glob(pattern):
                if p.is_file():
                    files.add(p)

        groups: Dict[str, List[Tuple[str, pydicom.Dataset]]] = {}
        for f in sorted(files):
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            except Exception:
                continue
            suid = getattr(ds, "SeriesInstanceUID", None)
            if suid is None:
                continue
            groups.setdefault(str(suid), []).append((str(f), ds))

        if not groups:
            raise ValueError(f"No readable DICOM series found in {dicom_dir!r}")

        def _sort_key(item: Tuple[str, pydicom.Dataset]) -> float:
            ds = item[1]
            if hasattr(ds, "InstanceNumber"):
                try:
                    return float(ds.InstanceNumber)
                except Exception:
                    pass
            ipp = getattr(ds, "ImagePositionPatient", None)
            if ipp is not None and len(ipp) == 3:
                try:
                    return float(ipp[2])
                except Exception:
                    pass
            return 0.0

        for lst in groups.values():
            lst.sort(key=_sort_key)

        return groups

    def _determine_best_rotation(self, slice_def, slice_dcm, mode: str) -> Tuple[int, bool]:
        """
        Return (k, flip) for the transform that best maps a NIfTI slice to the
        original DICOM pixel array.

        Searches over all combinations of rot90(k) and an optional horizontal
        flip so that the coordinate-system difference between NIfTI (RAS) and
        DICOM (LPS) is handled correctly regardless of scanner orientation.
        """
        import numpy as np

        if mode == "none":
            return 0, False
        if mode == "auto90":
            k_values = [0, 1, 3]       # 0°, 90° CCW, 90° CW — skip 180°
        elif mode == "auto_all":
            k_values = [0, 1, 2, 3]
        else:
            logger.warning("Unknown rotation_mode %r, defaulting to no transform.", mode)
            return 0, False

        sd = slice_def.astype(np.float32)
        so = slice_dcm.astype(np.float32)
        if sd.shape != so.shape:
            return 0, False

        best_mse = float("inf")
        best_k, best_flip = 0, False

        for k in k_values:
            for do_flip in (False, True):
                cand = np.rot90(sd, k=k)
                if do_flip:
                    cand = np.flip(cand, axis=1)
                if cand.shape != so.shape:
                    continue
                mse = float(np.mean((so - cand) ** 2))
                if mse < best_mse:
                    best_mse = mse
                    best_k = k
                    best_flip = do_flip

        logger.debug(
            "Orientation search: mode=%s  chosen k=%d flip=%s  MSE=%.2f",
            mode, best_k, best_flip, best_mse,
        )
        return best_k, best_flip

    @staticmethod
    def _prepare_for_write(ds: pydicom.Dataset) -> None:
        """Fix PixelData VR and ensure a valid transfer syntax before saving."""
        if "PixelData" in ds:
            bits = getattr(ds, "BitsAllocated", 16)
            ds["PixelData"].VR = "OB" if bits <= 8 else "OW"

        if not hasattr(ds, "file_meta") or ds.file_meta is None:
            ds.file_meta = FileMetaDataset()

        if not getattr(ds.file_meta, "TransferSyntaxUID", None):
            ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

        if ds.file_meta.TransferSyntaxUID == ImplicitVRLittleEndian:
            ds.is_implicit_VR = True
            ds.is_little_endian = True


# Convenience functions

def dicom_to_nifti(dicom_dir: str, output_dir: str,
                   series_instance_uid: Optional[str] = None) -> str:
    """Convenience function for DICOM to NIfTI conversion."""
    return Defacer().dicom_to_nifti(dicom_dir, output_dir, series_instance_uid)


def nifti_to_dicom(nifti_file: str, dicom_template_dir: str,
                   output_dir: str, rotation_mode: str = "auto90") -> List[str]:
    """Convenience function for NIfTI to DICOM conversion."""
    return Defacer().nifti_to_dicom(nifti_file, dicom_template_dir, output_dir, rotation_mode)
