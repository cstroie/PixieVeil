import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import pynetdicom
from pydicom.dataset import Dataset

from pixieveil.config.settings import Settings

logger = logging.getLogger(__name__)


class DicomServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ae = None
        self.ae_port = settings.dicom_server.get("port", 11112)

    async def start(self):
        """
        Start the DICOM server.
        """
        logger.info("Starting DICOM server")
        self.ae = pynetdicom.AE(ae_title=self.settings.dicom_server["ae_title"],
                                port=self.ae_port)
        self.ae.add_supported_context(pynetdicom.uid.ImplicitVRLittleEndian)
        self.ae.add_scp_handler(pynetdicom.sop_class.VerificationSOPClass,
                                self._handle_echo)
        self.ae.add_scp_handler(pynetdicom.sop_class.CTImageStorage,
                                self._handle_c_store)
        self.ae.add_scp_handler(pynetdicom.sop_class.MRImageStorage,
                                self._handle_c_store)
        self.ae.add_scp_handler(pynetdicom.sop_class.SecondaryCaptureImageStorage,
                                self._handle_c_store)

        try:
            self.ae.start()
            logger.info(f"DICOM server started on port {self.ae_port}")
        except Exception as e:
            logger.error(f"Failed to start DICOM server: {e}")
            raise

    async def stop(self):
        """
        Stop the DICOM server.
        """
        logger.info("Stopping DICOM server")
        if self.ae:
            self.ae.shutdown()
            self.ae = None
        logger.info("DICOM server stopped")

    def _handle_echo(self, assoc: "pynetdicom.association.Association",
                     context: "pynetdicom.presentation.PresentationContext",
                     info: "pynetdicom.presentation.PresentationContextInfo") -> int:
        """
        Handle C-ECHO requests.
        """
        logger.info("Received C-ECHO request")
        return 0x0000  # Success

    def _handle_c_store(self, assoc: "pynetdicom.association.Association",
                        context: "pynetdicom.presentation.PresentationContext",
                        info: "pynetdicom.presentation.PresentationContextInfo") -> int:
        """
        Handle C-STORE requests.
        """
        logger.info("Received C-STORE request")
        # TODO: Implement C-STORE handling
        return 0x0000  # Success
