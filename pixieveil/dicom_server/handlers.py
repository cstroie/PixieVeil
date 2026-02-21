"""
DICOM Server Handlers Module

This module provides handlers for DICOM server operations including C-STORE, C-ECHO,
and other DICOM service class provider (SCP) operations.

Classes:
    CStoreSCPHandler: Handles C-STORE SCP requests for receiving DICOM images
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom
import pynetdicom

from pixieveil.config import Settings
from pixieveil.storage.storage_manager import StorageManager

logger = logging.getLogger(__name__)


class CStoreSCPHandler:
    """
    Handles C-STORE SCP (Service Class Provider) requests from DICOM modalities.
    
    This class processes incoming DICOM images received via C-STORE requests,
    validates them, converts them to proper DICOM format, and forwards them
    to the storage manager for further processing.
    
    Attributes:
        settings (Settings): Application configuration settings
        storage_manager (StorageManager): Storage manager for handling DICOM image processing
    """
    
    def __init__(self, settings: Settings, storage_manager: StorageManager):
        """
        Initialize the CStoreSCPHandler with application settings and storage manager.
        
        Args:
            settings: Application configuration settings
            storage_manager: Storage manager for handling DICOM image processing
        """
        self.settings = settings
        self.storage_manager = storage_manager

    def handle_c_store(self, assoc: "pynetdicom.association.Association",
                           context: "pynetdicom.presentation.PresentationContext",
                           dataset: pydicom.Dataset,
                           file_meta: pydicom.Dataset) -> int:
        """
        Handle a C-STORE request from a DICOM modality.
        
        This method processes incoming DICOM images received via C-STORE requests,
        validates them, and forwards them to the storage manager for further processing.
        
        Args:
            assoc: DICOM association object
            context: Presentation context for the C-STORE request
            req: C-STORE request object
            
        Returns:
            int: Status code indicating success or failure
        """
        try:
            # Get a unique identifier for this image
            image_id = f"{dataset.SOPInstanceUID}"

            # Create a new DICOM dataset with file meta and pixel data
            ds = pydicom.Dataset()
            ds.file_meta = file_meta
            ds.update(dataset)
            
            # Save the DICOM image to temporary storage
            temp_path = self.storage_manager.save_temp_image(ds, image_id)

            # Process the image through the storage manager
            self.storage_manager.process_image(temp_path, image_id)
            
            logger.info(f"Successfully received image {image_id}")
            return 0x0000  # Success
            
        except Exception as e:
            logger.error(f"Failed to process C-STORE request: {e}")
            return 0xC000  # Failure

    def _validate_dicom(self, ds: pydicom.Dataset) -> bool:
        """
        Validate the DICOM image for required fields and basic integrity.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to validate
            
        Returns:
            bool: True if the DICOM dataset is valid, False otherwise
        """
        # Basic validation
        required_fields = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"]
        for field in required_fields:
            if field not in ds:
                return False

        return True
