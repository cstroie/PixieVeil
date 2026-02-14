import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import pynetdicom
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    Verification,
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage
)
from pydicom.dataset import Dataset

from pixieveil.config.settings import Settings
from pixieveil.storage.storage_manager import StorageManager
from pixieveil.dicom_server.handlers import CStoreSCPHandler

logger = logging.getLogger(__name__)


class DicomServer:
    def __init__(self, settings: Settings, storage_manager: StorageManager):
        self.settings = settings
        self.storage_manager = storage_manager
        self.ae = None
        self.ae_port = settings.dicom_server.get("port", 11112)
        self.server_task = None
        self.c_store_handler = CStoreSCPHandler(settings, storage_manager)

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

        # Add supported contexts (already present above)
        
        # Register event handlers using proper API
        handlers = [
            (evt.EVT_C_ECHO, self._handle_echo),
            (evt.EVT_C_STORE, self._handle_c_store)
        ]

        # Start the server in a separate thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        self.server_task = loop.run_in_executor(
            None, 
            self._start_blocking_server,
            handlers  # Pass handlers to server starter
        )
        logger.info(f"DICOM server starting on port {self.ae_port}")

    def _start_blocking_server(self, handlers):
        """
        Start the DICOM server (blocking call).
        """
        try:
            self.ae.start_server(('', self.ae_port), evt_handlers=handlers)
            logger.info(f"DICOM server started on port {self.ae_port}")
        except Exception as e:
            logger.error(f"Failed to start DICOM server: {e}")
            raise

    async def stop(self):
        """
        Stop the DICOM server gracefully.
        """
        logger.info("Stopping DICOM server")
        try:
            if self.server_task:
                if not self.server_task.done():
                    self.server_task.cancel()
                    try:
                        await self.server_task
                    except asyncio.CancelledError:
                        logger.debug("DICOM server task cancelled")
            
            if self.ae:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.ae.shutdown)
                self.ae = None
                logger.info("DICOM server stopped")
        except Exception as e:
            logger.error(f"Error stopping DICOM server: {e}")
            raise

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
                {"dataset": event.dataset, "file_meta": event.file_meta}
            )
        except Exception as e:
            logger.error(f"Error handling C-STORE request: {e}")
            return 0x0106  # Out of resources
