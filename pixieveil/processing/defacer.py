"""
DICOM defacing module for PixieVeil.

This module provides DICOM to NIfTI conversion and NIfTI to DICOM conversion
capabilities for implementing a defacing pipeline. The full defacing workflow
(DICOM → NIfTI → defaced NIfTI → DICOM) is orchestrated by deface_series().
"""

import logging
import re
import shutil
import tempfile
import threading
import warnings
from pathlib import Path

import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian

logger = logging.getLogger(__name__)

# DICOM tags used for head-scan detection
_HEAD_BODY_PARTS = {"HEAD", "BRAIN", "NECK", "SKULL"}


class Defacer:
    """DICOM defacing conversion utilities."""

    # One nnUNet inference at a time — class-level so all instances share it.
    _nnunet_semaphore = threading.Semaphore(1)

    def __init__(self, config: dict | None = None, temp_path: Path | None = None):
        """
        Args:
            config: The ``defacing`` section of Settings, or None to use defaults.
            temp_path: Base directory for temporary NIfTI work; defaults to system temp.
        """
        cfg = config or {}
        self.enabled: bool = cfg.get("enabled", False)
        self.keep_backup: bool = cfg.get("keep_backup", True)
        self.rotation_mode: str = cfg.get("rotation_mode", "iop")
        self.mask_dilation_mm: float = float(cfg.get("mask_dilation_mm", 2.0))
        self.temp_path: Path | None = temp_path

        raw_model_dir = cfg.get("model_dir", None)
        self.model_dir: Path | None = Path(raw_model_dir) if raw_model_dir else None

        body_parts = cfg.get("body_parts", list(_HEAD_BODY_PARTS))
        self._body_parts: set = {bp.upper() for bp in body_parts}

        desc_pattern = cfg.get("series_description_pattern",
                               r"(?i)(head|brain|skull|cranial|cerebr)")
        self._desc_re: re.Pattern = re.compile(desc_pattern)

        self.device: str = self._resolve_device(cfg.get("device", "cuda"))

    # ------------------------------------------------------------------
    # Device resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_device(requested: str) -> str:
        """
        Validate the requested torch device and fall back to CPU if unavailable.

        Returns the effective device string (``"cuda"``, ``"mps"``, or ``"cpu"``).
        """
        try:
            import torch
        except ImportError:
            # torch not installed — defacing will fail later with a clear error
            return requested

        if requested == "cuda":
            if torch.cuda.is_available():
                try:
                    # Attempt a tiny allocation to verify the device really works
                    torch.zeros(1, device="cuda")
                    logger.info("CUDA is available and functional — using GPU for defacing")
                    return "cuda"
                except Exception as exc:
                    logger.warning(
                        "CUDA reported as available but a test allocation failed (%s); "
                        "falling back to CPU for defacing",
                        exc,
                    )
                    return "cpu"
            else:
                logger.warning(
                    "Defacing device configured as 'cuda' but CUDA is not available; "
                    "falling back to CPU — defacing will be significantly slower"
                )
                return "cpu"

        if requested == "mps":
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                logger.info("MPS is available — using Apple GPU for defacing")
                return "mps"
            else:
                logger.warning(
                    "Defacing device configured as 'mps' but MPS is not available; "
                    "falling back to CPU"
                )
                return "cpu"

        return "cpu"

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

    _TOPOGRAM_DESC_RE = re.compile(
        r"(?i)(topogram|scout|scanogram|localizer|surview|overview|pilot)"
    )

    def is_topogram(self, series_dir: Path) -> bool:
        """
        Return True if the series is a topogram / scout / localizer.

        Checks (in order, on the first readable DICOM file):
        - ImageType contains ``LOCALIZER``
        - SeriesDescription matches the topogram pattern
        - ScanOptions contains ``TOPOGRAM``
        """
        for f in sorted(series_dir.iterdir()):
            if not f.is_file():
                continue
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            except Exception:
                continue

            image_type = [str(v).upper() for v in getattr(ds, "ImageType", [])]
            if "LOCALIZER" in image_type:
                logger.debug("Topogram detected by ImageType=LOCALIZER in %s", series_dir)
                return True

            description = str(getattr(ds, "SeriesDescription", ""))
            if description and self._TOPOGRAM_DESC_RE.search(description):
                logger.debug(
                    "Topogram detected by SeriesDescription=%r in %s", description, series_dir
                )
                return True

            scan_options = str(getattr(ds, "ScanOptions", "")).upper()
            if "TOPOGRAM" in scan_options:
                logger.debug("Topogram detected by ScanOptions in %s", series_dir)
                return True

            break

        return False

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def deface_series(self, series_dir: Path,
                      data_dir: Path | None = None) -> bool:
        """
        Run the full defacing pipeline for one series directory.

        Steps:
        1. Convert DICOM series → NIfTI
        2. Run nnUNet inference to produce a face mask and apply it
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

        # Step 2: external defacing tool (or nnUNet inference)
        try:
            defaced_nifti = self._run_defacing_tool(
                Path(nifti_path), nifti_in_dir, nifti_out_dir, data_dir=data_dir
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

        # Step 4: atomic swap with optional backup.
        # Stage defaced files to a sibling directory first so that the original
        # series is only removed after the copy succeeds.
        backup_dir = series_dir.parent / f"{series_dir.name}_pre_deface"
        staged_dir = series_dir.parent / f"{series_dir.name}_staged"
        try:
            shutil.copytree(dicom_out_dir, staged_dir)
        except Exception as e:
            logger.error("Staging defaced series failed for %s: %s", series_dir, e)
            shutil.rmtree(staged_dir, ignore_errors=True)
            return False

        try:
            if self.keep_backup:
                series_dir.rename(backup_dir)
                logger.info("Backup of anonymized series kept at %s", backup_dir)
            else:
                shutil.rmtree(series_dir)

            staged_dir.rename(series_dir)
        except Exception as e:
            logger.error("Atomic swap failed for %s: %s", series_dir, e)
            shutil.rmtree(staged_dir, ignore_errors=True)
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

    def _ensure_model(self, data_dir: Path | None = None) -> Path:
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
           (e.g. ``./data/nnUNet`` when ``base_path`` is ``./data/dicom``).

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
                             data_dir: Path | None = None,
                             device: str = "cpu") -> None:
        """
        Run nnUNetv2_predict on all cases in nifti_in_dir.

        The nnUNet model is expected at ``<model_dir>/nnUNet``, where
        ``model_dir`` comes from config (``defacing.model_dir``) or falls back
        to ``<data_dir>.parent/nnUNet`` when *data_dir* is supplied.

        Calls ``nnUNetPredictor`` directly — no subprocess.  The model folder
        is resolved from ``model_root`` and the
        ``nnUNetTrainer__nnUNetPlans__3d_fullres`` configuration is assumed.

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
        import os
        model_root = self._ensure_model(data_dir)

        nifti_in_dir.mkdir(parents=True, exist_ok=True)
        nifti_out_dir.mkdir(parents=True, exist_ok=True)

        # Must be set before nnunetv2 is imported — it reads them at module level.
        os.environ.setdefault("nnUNet_results",      str(model_root))
        os.environ.setdefault("nnUNet_preprocessed", str(model_root))
        os.environ.setdefault("nnUNet_raw",          str(model_root))

        import torch
        from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

        logger.info(
            "nnU-Net citation: Isensee F, Jaeger PF, Kohl SAA, Petersen J, Maier-Hein KH. "
            "nnU-Net: a self-configuring method for deep learning-based biomedical image "
            "segmentation. Nat Methods. 2021;18(2):203-211. doi:10.1038/s41592-020-01008-z"
        )

        torch_device = torch.device(device)
        use_mirroring = device != "cpu"  # mirroring TTA is slow on CPU

        model_folder = str(model_root / self._MODEL_DATASET / "nnUNetTrainer__nnUNetPlans__3d_fullres")

        logger.info(
            "Running nnUNet inference: model=%s device=%s mirroring=%s",
            model_folder, device, use_mirroring,
        )
        perform_on_device = torch_device.type == "cuda"

        logger.debug("Waiting for nnUNet semaphore")
        with self._nnunet_semaphore:
            logger.debug("nnUNet semaphore acquired")
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=".*old nnU-Net plans format.*",
                    category=UserWarning,
                )
                predictor = nnUNetPredictor(
                    tile_step_size=0.5,
                    use_gaussian=True,
                    use_mirroring=use_mirroring,
                    perform_everything_on_device=perform_on_device,
                    device=torch_device,
                    verbose=False,
                    allow_tqdm=True,
                )
                predictor.initialize_from_trained_model_folder(
                    model_folder,
                    use_folds=("all",),
                    checkpoint_name="checkpoint_final.pth",
                )
            predictor.predict_from_files(
                str(nifti_in_dir),
                str(nifti_out_dir),
                save_probabilities=False,
                overwrite=True,
            )

        logger.info("nnUNet inference complete; output in %s", nifti_out_dir)

    def _run_defacing_tool(self, nifti_path: Path,
                           nifti_in_dir: Path, nifti_out_dir: Path,
                           data_dir: Path | None = None) -> Path:
        """
        Run nnUNet inference to produce a face-region mask, apply it, and
        return the path to the defaced NIfTI.
        """
        return self._run_nnunet_and_apply_mask(
            nifti_path, nifti_in_dir, nifti_out_dir, data_dir=data_dir,
            device=self.device,
        )

    def _run_nnunet_and_apply_mask(self, nifti_path: Path,
                                   nifti_in_dir: Path, nifti_out_dir: Path,
                                   data_dir: Path | None = None,
                                   device: str = "cpu") -> Path:
        """
        Run nnUNet inference to get a face-region mask, then apply it.

        nnUNet writes one prediction file per input case (same stem, no _0000).
        We locate the matching prediction, binarise it (>0 = face), and set
        those voxels in the original volume to its 1st-percentile intensity (background air).

        Returns the path to the saved defaced NIfTI.
        """
        import nibabel as nib
        import numpy as np

        self.run_nnunet_inference(nifti_in_dir, nifti_out_dir, data_dir=data_dir, device=device)

        # Derive the case stem from the input path (strip _0000.nii.gz or .nii.gz/.nii)
        fname = nifti_path.name
        for suffix in ("_0000.nii.gz", "_0000.nii", ".nii.gz", ".nii"):
            if fname.endswith(suffix):
                case_stem = fname[: -len(suffix)]
                break
        else:
            case_stem = nifti_path.stem

        # Locate the nnUNet prediction for this case
        mask_path: Path | None = None
        for candidate in sorted(nifti_out_dir.glob("*.nii*")):
            if candidate.name.startswith(case_stem):
                mask_path = candidate
                break
        if mask_path is None:
            # Fall back to the first prediction file present
            predictions = sorted(nifti_out_dir.glob("*.nii*"))
            if not predictions:
                raise RuntimeError(
                    f"nnUNet produced no prediction in {nifti_out_dir}"
                )
            mask_path = predictions[0]
            logger.warning(
                "No prediction matching stem %r; using %s", case_stem, mask_path
            )

        logger.info("Applying face mask from %s to %s", mask_path, nifti_path)

        mask_img = nib.load(str(mask_path))
        mask = (np.asarray(mask_img.get_fdata()) > 0)

        if self.mask_dilation_mm > 0:
            mask = self._dilate_mask(mask, mask_img, self.mask_dilation_mm)

        orig_img = nib.load(str(nifti_path))
        orig_data = np.asarray(orig_img.get_fdata())

        # 1st percentile captures background air reliably without pulling in tissue.
        fill_value = float(np.percentile(orig_data, 1))
        logger.debug("Face fill value (1st percentile): %.1f", fill_value)

        defaced_data = np.where(mask, fill_value, orig_data)
        defaced_img = nib.Nifti1Image(defaced_data, orig_img.affine, orig_img.header)

        defaced_path = nifti_out_dir / f"{case_stem}_defaced.nii.gz"
        nib.save(defaced_img, str(defaced_path))
        logger.info("Defaced NIfTI saved to %s", defaced_path)

        return defaced_path

    # ------------------------------------------------------------------
    # DICOM → NIfTI
    # ------------------------------------------------------------------

    def dicom_to_nifti(self, dicom_dir: str, output_dir: str,
                       series_instance_uid: str | None = None) -> str:
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
                       output_dir: str, rotation_mode: str = "iop") -> list[str]:
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
            rotation_mode: "none" to skip, anything else uses IOP-based transpose (default "iop")

        Returns:
            list[str]: Paths to created DICOM files

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
        else:
            ref_dtype = np.int16

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

        if rotation_mode == "none":
            best_k, best_flip = 0, False
        else:
            iop = getattr(sample_ds, "ImageOrientationPatient", None)
            if iop is not None:
                best_k, best_flip = self._rotation_from_iop([float(v) for v in iop])
            else:
                logger.warning(
                    "ImageOrientationPatient missing from %s; defaulting to transpose",
                    chosen_list[0][0],
                )
                best_k, best_flip = 1, True

        created_files: list[str] = []

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

        # chosen_list entries were loaded stop_before_pixels=True; re-read with
        # full pixel data for slices that were not replaced by the defaced volume.
        for src_path, _ in chosen_list[n_update:]:
            ds_full = pydicom.dcmread(src_path, force=True)
            self._prepare_for_write(ds_full)
            out_path = output_dir / Path(src_path).name
            ds_full.save_as(str(out_path))
            created_files.append(str(out_path))

        logger.info("Wrote %d DICOM slices to %s", len(created_files), output_dir)
        return created_files

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_series_groups(
        self, dicom_dir: Path
    ) -> dict[str, list[tuple[str, pydicom.Dataset]]]:
        """Load all DICOMs in dicom_dir grouped by SeriesInstanceUID, sorted by position."""
        files: set = set()
        for pattern in ("*.dcm", "*"):
            for p in dicom_dir.glob(pattern):
                if p.is_file():
                    files.add(p)

        groups: dict[str, list[tuple[str, pydicom.Dataset]]] = {}
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

        def _sort_key(item: tuple[str, pydicom.Dataset]) -> float:
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

    @staticmethod
    def _dilate_mask(mask: "np.ndarray", mask_img: "nib.Nifti1Image",
                     dilation_mm: float) -> "np.ndarray":
        """
        Grow a binary mask by *dilation_mm* millimetres using a spherical
        structuring element sized from the image voxel spacing.

        Uses scipy.ndimage.binary_dilation when available; falls back to a
        slower numpy-only box dilation otherwise.
        """
        import numpy as np

        zooms = np.array(mask_img.header.get_zooms()[:3], dtype=float)
        # Radius in voxels per axis, minimum 1
        radii = np.maximum(np.round(dilation_mm / zooms).astype(int), 1)

        try:
            from scipy.ndimage import binary_dilation

            # Build an ellipsoidal (spherical for isotropic) footprint
            slices = tuple(slice(-r, r + 1) for r in radii)
            coords = np.mgrid[slices]
            footprint = sum((coords[ax] / radii[ax]) ** 2 for ax in range(3)) <= 1.0
            dilated = binary_dilation(mask, structure=footprint)
            logger.debug(
                "Mask dilated by %.1f mm (radii %s vox) using scipy", dilation_mm, radii
            )
        except ImportError:
            # Fallback: repeated single-step dilation along each axis.
            # One step ORs each voxel with its two face-neighbours on that axis.
            dilated = mask.copy()
            for ax, r in enumerate(radii):
                for _ in range(int(r)):
                    fwd = np.roll(dilated, 1, axis=ax)
                    bwd = np.roll(dilated, -1, axis=ax)
                    # Zero out wrap-around edges introduced by roll
                    idx_fwd = [slice(None)] * 3; idx_fwd[ax] = slice(0, 1)
                    idx_bwd = [slice(None)] * 3; idx_bwd[ax] = slice(-1, None)
                    fwd[tuple(idx_fwd)] = False
                    bwd[tuple(idx_bwd)] = False
                    dilated = dilated | fwd | bwd
            logger.debug(
                "Mask dilated by %.1f mm (radii %s vox) using numpy fallback", dilation_mm, radii
            )

        return dilated

    @staticmethod
    def _rotation_from_iop(iop: list[float]) -> tuple[int, bool]:
        """
        Return the (k, flip) that maps a 2D NIfTI slice to DICOM pixel_array orientation.

        SimpleITK writes NIfTI with axis 0 = row cosines (F) and axis 1 = column
        cosines (C).  DICOM pixel_array has axis 0 = rows (C direction) and axis 1 =
        cols (F direction).  The LPS↔RAS conversion preserves the anatomical direction
        of both vectors, so IPP anchors i=0↔col=0 and j=0↔row=0.  The in-plane
        transform is therefore always a pure transpose:

            np.rot90(arr, k=1) then flip axis=1  ≡  arr.T

        The IOP is validated and logged; the return value is always (1, True).
        """
        import numpy as np

        F = np.array(iop[:3], dtype=float)
        C = np.array(iop[3:], dtype=float)
        norm_F, norm_C = np.linalg.norm(F), np.linalg.norm(C)

        if norm_F > 1e-6 and norm_C > 1e-6:
            dot = abs(float(np.dot(F / norm_F, C / norm_C)))
            if dot > 0.3:
                logger.warning(
                    "IOP row/col cosines are not orthogonal (|dot|=%.3f); "
                    "proceeding with transpose anyway",
                    dot,
                )
            else:
                logger.debug("IOP F=%s C=%s → transpose", np.round(F, 3), np.round(C, 3))
        else:
            logger.warning("IOP vectors are near-zero (|F|=%.3f |C|=%.3f); using transpose", norm_F, norm_C)

        # Pure transpose: rot90(k=1) then flip axis=1 gives result[i,j] = arr[j,i]
        return 1, True

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



# Convenience functions

def dicom_to_nifti(dicom_dir: str, output_dir: str,
                   series_instance_uid: str | None = None) -> str:
    """Convenience function for DICOM to NIfTI conversion."""
    return Defacer().dicom_to_nifti(dicom_dir, output_dir, series_instance_uid)


def nifti_to_dicom(nifti_file: str, dicom_template_dir: str,
                   output_dir: str, rotation_mode: str = "auto90") -> list[str]:
    """Convenience function for NIfTI to DICOM conversion."""
    return Defacer().nifti_to_dicom(nifti_file, dicom_template_dir, output_dir, rotation_mode)
