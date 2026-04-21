"""
DICOM defacing module for PixieVeil.

This module provides DICOM to NIfTI conversion and NIfTI to DICOM conversion
capabilities for implementing a defacing pipeline. The full defacing workflow
(DICOM → NIfTI → defaced NIfTI → DICOM) is not implemented here - this module
only provides the conversion steps.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import SimpleITK as sitk
import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian

logger = logging.getLogger(__name__)


class Defacer:
    """DICOM defacing conversion utilities."""

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

    def _load_series_groups(
        self, dicom_dir: Path
    ) -> Dict[str, List[Tuple[str, pydicom.Dataset]]]:
        """
        Load all DICOMs in dicom_dir and group by SeriesInstanceUID, sorted by
        InstanceNumber or z-position.
        """
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

    def _determine_best_rotation(self, slice_def: np.ndarray, slice_dcm: np.ndarray,
                                  mode: str) -> int:
        """
        Determine best np.rot90 k for matching defaced NIfTI slice to original DICOM.

        mode: "none" → k=0, "auto90" → search {0,1,3}, "auto_all" → search {0,1,2,3}
        """
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
            logger.debug("Slice shapes differ during rotation search; using k=0.")
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
        elif candidates_dim0:  # both match, prefer dim0
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

        # Reorient so slice axis is first
        arr_slices = arr if chosen_dim == 0 else np.moveaxis(arr, 2, 0)

        n_ref = len(chosen_list)
        n_nifti = arr_slices.shape[0]
        if n_nifti != n_ref:
            logger.warning(
                "NIfTI slices (%d) != DICOM slices (%d); updating overlapping subset.",
                n_nifti, n_ref
            )
        n_update = min(n_nifti, n_ref)

        # Match dtype from reference and determine rotation
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

        # Update overlapping slices
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

        # Copy remaining template slices unchanged
        for src_path, ds in chosen_list[n_update:]:
            self._prepare_for_write(ds)
            out_path = output_dir / Path(src_path).name
            ds.save_as(str(out_path))
            created_files.append(str(out_path))

        logger.info("Wrote %d DICOM slices to %s", len(created_files), output_dir)
        return created_files


# Convenience functions

def dicom_to_nifti(dicom_dir: str, output_dir: str,
                   series_instance_uid: Optional[str] = None) -> str:
    """Convenience function for DICOM to NIfTI conversion."""
    return Defacer().dicom_to_nifti(dicom_dir, output_dir, series_instance_uid)


def nifti_to_dicom(nifti_file: str, dicom_template_dir: str,
                   output_dir: str, rotation_mode: str = "auto90") -> List[str]:
    """Convenience function for NIfTI to DICOM conversion."""
    return Defacer().nifti_to_dicom(nifti_file, dicom_template_dir, output_dir, rotation_mode)
