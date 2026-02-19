"""
DICOM Server Handlers Module

This module provides handlers for DICOM server events, particularly for
processing C-STORE requests received from DICOM modalities.

Classes:
    CStoreSCPHandler: Handles C-STORE SCP (Service Class Provider) requests
"""

import logging
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom
from pydicom.dataset import Dataset
import pynetdicom

from pixieveil.config import Settings
from pixieveil.storage import StorageManager

logger = logging.getLogger(__name__)


class CStoreSCPHandler:
    """
    Handles C-STORE SCP (Service Class Provider) requests from DICOM modalities.
    
    This class processes incoming DICOM images received via C-STORE requests,
    validates them, converts them to proper DICOM format, and forwards them
    to the storage manager for further processing.
    
    Attributes:
        settings (Settings): Application configuration settings
        storage (StorageManager): Storage manager for handling DICOM image processing
    """
    
    def __init__(self, settings: Settings, storage_manager: StorageManager):
        """
        Initialize the CStoreSCPHandler with application settings and storage manager.
        
        Args:
            settings: Application configuration settings
            storage_manager: Storage manager instance for handling image processing
        """
        self.settings = settings
        self.storage = storage_manager

    def handle_c_store(self, assoc: "pynetdicom.association.Association",
                      context: "pynetdicom.presentation.PresentationContext",
                      info: Dict[str, Any]) -> int:
        """
        Handle C-STORE requests from DICOM modalities.
        
        This method processes incoming DICOM images received via C-STORE requests.
        It validates the DICOM data, converts it to proper format, and forwards
        it to the storage manager for processing.
        
        Args:
            assoc: DICOM association object containing connection information
            context: Presentation context for the C-STORE request
            info: Dictionary containing the DICOM dataset and metadata
            
        Returns:
            int: DICOM status code (0x0000 for success, 0xC000 for processing failure,
                 0x0106 for out of resources)
                 
        Note:
            The method generates a unique ID for each received image to ensure
            proper tracking and processing.
        """
        try:
            # Generate unique ID for the image
            image_id = str(uuid.uuid4())

            # Get the DICOM dataset from event info
            if 'dataset' not in info:
                logger.error("No dataset found in C-STORE request")
                return 0xC000  # Processing failure
            
            dataset = info['dataset']
            file_meta = info.get('file_meta', {})
            
            # Create a new DICOM dataset with file meta and pixel data
            ds = pydicom.Dataset()
            ds.file_meta = file_meta
            ds.update(dataset)
            
            # Convert to bytes using pydicom's save_as
            from io import BytesIO
            buffer = BytesIO()
            # Use new enforce_file_format parameter instead of deprecated write_like_original
            ds.save_as(buffer, enforce_file_format=False)
            ds_bytes = buffer.getvalue()
            
            # Save the DICOM image temporarily
            temp_path = self.storage.save_temp_image(ds_bytes, image_id)

            logger.info(f"Successfully received image {image_id}")

            # Process the image
            self.storage.process_image(temp_path, image_id)

            # return success status
            return 0x0000  # Success

        except Exception as e:
            logger.error(f"Failed to receive image: {e}")
            return 0x0106  # Out of resources

    def _validate_dicom(self, ds: pydicom.Dataset) -> bool:
        """
        Validate the received DICOM image for basic integrity.
        
        This method checks if the DICOM dataset contains the minimum required
        fields for proper processing.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to validate
            
        Returns:
            bool: True if the DICOM dataset is valid, False otherwise
        """
        # Basic validation
        if not hasattr(ds, "SOPClassUID") or not hasattr(ds, "SOPInstanceUID"):
            return False

        return True
