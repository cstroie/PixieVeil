import asyncio
import logging
from pathlib import Path
from typing import Dict, Any

import pynetdicom
from pynetdicom.sop_class import CTImageStorage

from pixieveil.config import Settings
from pixieveil.dicom_server.handlers import CStoreSCPHandler

logger = logging.getLogger(__name__)

class DicomServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ae = None
        self.c_store_handler = CStoreSCPHandler(settings)

    async def start(self):
        logger.info("Starting DICOM server")

        # Create Application Entity
        self.ae = pynetdicom.AE(
            ae_title=self.settings.dicom_server["ae_title"],
            port=self.settings.dicom_server["port"],
            ip=self.settings.dicom_server["ip"]
        )

        # Add supported presentation contexts
        self.ae.add_supported_context(CTImageStorage)

        # Add event handlers
        self.ae.on_c_store = self.c_store_handler.handle_c_store

        # Start the AE
        self.ae.start()

        logger.info(f"DICOM server started on {self.settings.dicom_server['ip']}:{self.settings.dicom_server['port']}")

    async def stop(self):
        logger.info("Stopping DICOM server")
        if self.ae:
            self.ae.stop()
        logger.info("DICOM server stopped")
