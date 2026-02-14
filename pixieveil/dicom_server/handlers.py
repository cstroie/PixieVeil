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
    def __init__(self, settings: Settings, storage_manager: StorageManager):
        self.settings = settings
        self.storage = storage_manager

    def handle_c_store(self, assoc: "pynetdicom.association.Association",
                      context: "pynetdicom.presentation.PresentationContext",
                      info: Dict[str, Any]) -> int:
        """
        Handle C-STORE requests from DICOM modalities.
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

            # Process the image
            self.storage.process_image(temp_path, image_id)

            logger.info(f"Successfully received image {image_id}")
            return 0x0000  # Success

        except Exception as e:
            logger.error(f"Failed to receive image: {e}")
            return 0x0106  # Out of resources

    def _validate_dicom(self, ds: pydicom.Dataset) -> bool:
        """
        Validate the received DICOM image.
        """
        # Basic validation
        if not hasattr(ds, "SOPClassUID") or not hasattr(ds, "SOPInstanceUID"):
            return False

        return True
