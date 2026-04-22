"""
DICOM Storage Module

Sends anonymized (and optionally defaced) DICOM files to a remote DICOM node
via C-STORE.  All files in a study directory are sent in a single association
for efficiency.  Pre-deface backup directories (_pre_deface) are skipped.

Configuration (under storage.remote_storage.dicom in settings.yaml)::

    dicom:
      host: "127.0.0.1"
      port: 4070
      ae_title: "ORTHANC"        # called AE title (remote node)
      calling_ae: "PIXIEVEIL"    # our AE title; defaults to dicom_server.ae_title
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import pydicom
from pynetdicom import AE
from pynetdicom.sop_class import (
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage,
)

from pixieveil.config import Settings

logger = logging.getLogger(__name__)

# SOP classes we offer as SCU
_PRESENTATION_CONTEXTS = [
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage,
]


class DicomStorage:
    """
    Sends a study directory to a remote DICOM node via C-STORE.

    ``send_study`` is a coroutine that offloads the blocking pynetdicom call
    to a thread pool, keeping the asyncio event loop free.
    """

    def __init__(self, settings: Settings):
        cfg = settings.storage.get("remote_storage", {}).get("dicom", {})
        self.host: Optional[str] = cfg.get("host")
        self.port: Optional[int] = cfg.get("port")
        self.ae_title: str = cfg.get("ae_title", "ANY-SCP")
        self.calling_ae: str = cfg.get(
            "calling_ae",
            settings.dicom_server.get("ae_title", "PIXIEVEIL"),
        )
        self.enabled: bool = bool(self.host and self.port)

    async def send_study(self, study_dir: Path) -> bool:
        """
        Send all DICOM files in *study_dir* to the configured remote node.

        Skips ``*_pre_deface`` sub-directories.  Runs the blocking pynetdicom
        call in a thread pool so the event loop stays free.

        Returns True if every file was sent successfully, False otherwise.
        """
        if not self.enabled:
            return False
        return await asyncio.to_thread(self._send_study_sync, study_dir)

    def _send_study_sync(self, study_dir: Path) -> bool:
        """Blocking C-STORE transfer — runs in a thread pool."""
        dcm_files = [
            f for f in sorted(study_dir.rglob("*.dcm"))
            if f.is_file()
            and not any(part.endswith("_pre_deface") for part in f.parts)
        ]
        if not dcm_files:
            logger.warning("No DICOM files to send in %s", study_dir)
            return False

        ae = AE(ae_title=self.calling_ae)
        for ctx in _PRESENTATION_CONTEXTS:
            ae.add_requested_context(ctx)

        logger.info(
            "Connecting to DICOM node %s:%d (AE=%s) to send %d files from %s",
            self.host, self.port, self.ae_title, len(dcm_files), study_dir,
        )

        assoc = ae.associate(self.host, self.port, ae_title=self.ae_title)
        if not assoc.is_established:
            logger.error(
                "Failed to associate with %s:%d (AE=%s)",
                self.host, self.port, self.ae_title,
            )
            return False

        errors = 0
        try:
            for dcm_path in dcm_files:
                try:
                    ds = pydicom.dcmread(str(dcm_path))
                except Exception as exc:
                    logger.error("Cannot read %s: %s", dcm_path, exc)
                    errors += 1
                    continue

                status = assoc.send_c_store(ds)
                if status and status.Status == 0x0000:
                    logger.debug("C-STORE OK: %s", dcm_path.name)
                else:
                    code = status.Status if status else "no-response"
                    logger.error("C-STORE failed for %s: status=0x%04X", dcm_path.name, code)
                    errors += 1
        finally:
            assoc.release()

        if errors:
            logger.error(
                "DICOM send for %s finished with %d error(s) out of %d files",
                study_dir, errors, len(dcm_files),
            )
            return False

        logger.info(
            "DICOM send complete: %d files sent to %s:%d",
            len(dcm_files), self.host, self.port,
        )
        return True
