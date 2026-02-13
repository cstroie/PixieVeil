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
from pixieveil.dicom_server.handlers import CStoreSCPHandler

logger = logging.getLogger(__name__)


class DicomServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ae = None
        self.ae_port = settings.dicom_server.get("port", 11112)
        self.server_task = None
        self.c_store_handler = CStoreSCPHandler(settings)

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

        # Register event handlers using the correct pynetdicom API
        # Use the event IDs directly instead of add_notification_handler
        self.ae._handlers[1] = self._handle_echo  # C-ECHO
        self.ae._handlers[3] = self._handle_c_store  # C-STORE

        # Start the server in a separate thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        self.server_task = loop.run_in_executor(
            None, 
            self._start_blocking_server
        )
        logger.info(f"DICOM server starting on port {self.ae_port}")

    def _start_blocking_server(self):
        """
        Start the DICOM server (blocking call).
        """
        try:
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
        if self.server_task:
            # Cancel the server task
            self.server_task.cancel()
            try:
                await self.server_task
            except asyncio.CancelledError:
                pass
        
        if self.ae:
            # Shutdown in executor to avoid blocking
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.ae.shutdown)
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
        try:
            # Use the C-STORE handler to process the request
            return self.c_store_handler.handle_c_store(
                event.assoc, 
                event.context, 
                {"pdvs": [event.file_meta] + event.dataset}
            )
        except Exception as e:
            logger.error(f"Error handling C-STORE request: {e}")
            return 0x0106  # Out of resources
