import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import pynetdicom
from pynetdicom import AE
from pynetdicom.sop_class import (
    Verification,
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage
)
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
        self.ae = AE(ae_title=self.settings.dicom_server["ae_title"])
        self.ae.port = self.ae_port
        
        # Add supported contexts
        self.ae.add_supported_context(Verification)
        self.ae.add_supported_context(CTImageStorage)
        self.ae.add_supported_context(MRImageStorage)
        self.ae.add_supported_context(SecondaryCaptureImageStorage)

        try:
            # Use the correct method to start the server
            self.ae.start_server(('', self.ae_port))
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

    def _handle_echo(self, event: "pynetdicom.events.Event") -> int:
        """
        Handle C-ECHO requests.
        """
        logger.info("Received C-ECHO request")
        return 0x0000  # Success

    def _handle_c_store(self, event: "pynetdicom.events.Event") -> int:
        """
        Handle C-STORE requests.
        """
        logger.info("Received C-STORE request")
        # TODO: Implement C-STORE handling
        return 0x0000  # Success
