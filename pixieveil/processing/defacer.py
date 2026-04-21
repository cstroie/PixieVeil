"""
DICOM defacing module for PixieVeil.

This module provides DICOM to NIfTI conversion and NIfTI to DICOM conversion
capabilities for implementing a defacing pipeline. The full defacing workflow
(DICOM → NIfTI → defaced NIfTI → DICOM) is orchestrated by deface_series().
"""

import logging
import re
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

    def _run_defacing_tool(self, nifti_path: Path,
                           nifti_in_dir: Path, nifti_out_dir: Path) -> Path:
        """
        Call the external defacing tool, or simulate it by copying the NIfTI.

        Returns the path to the defaced NIfTI file.
        """
        if self.tool_command is None:
            # Simulation: copy the input NIfTI to the output dir unchanged
            defaced = nifti_out_dir / nifti_path.name
            shutil.copy2(nifti_path, defaced)
            logger.info("Defacing simulated (tool_command not set): copied %s → %s",
                        nifti_path.name, defaced)
            return defaced

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

        arr_slices = arr_slices.astype(ref_dtype)

        if sample_orig_slice is not None:
            mid = n_update // 2
            best_k = self._determine_best_rotation(arr_slices[mid], sample_orig_slice, rotation_mode)
        else:
            best_k = 0

        created_files: List[str] = []

        for (src_path, ds), slice_data in zip(
            chosen_list[:n_update], arr_slices[:n_update]
        ):
            slice_arr = np.rot90(np.asarray(slice_data), k=best_k)
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

    def _determine_best_rotation(self, slice_def, slice_dcm, mode: str) -> int:
        """Return best np.rot90 k for matching defaced NIfTI slice to original DICOM."""
        import numpy as np

        if mode == "none":
            return 0
        if mode == "auto90":
            allowed = [0, 1, 3]
        elif mode == "auto_all":
            allowed = [0, 1, 2, 3]
        else:
            logger.warning("Unknown rotation_mode %r, defaulting to k=0.", mode)
            return 0

        sd = slice_def.astype(np.float32)
        so = slice_dcm.astype(np.float32)
        if sd.shape != so.shape:
            return 0

        errors = {}
        for k in allowed:
            cand = np.rot90(sd, k=k)
            if cand.shape == so.shape:
                diff = so - cand
                errors[k] = float(np.mean(diff * diff))

        if not errors:
            return 0

        best_k = min(errors, key=errors.__getitem__)
        logger.debug("Rotation search: mode=%s errors=%s chosen k=%d", mode, errors, best_k)
        return best_k

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
