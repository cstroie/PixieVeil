import logging
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom
from pydicom.dataset import FileMetaDataset

from pixieveil.config import Settings
from pixieveil.storage import StorageManager

logger = logging.getLogger(__name__)

class CStoreSCPHandler:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = StorageManager(settings)

    def handle_c_store(self, assoc: "pynetdicom.association.Association",
                      context: "pynetdicom.presentation.PresentationContext",
                      info: Dict[str, Any]) -> int:
        """
        Handle C-STORE requests from DICOM modalities.
        """
        try:
            # Generate unique ID for the image
            image_id = str(uuid.uuid4())

            # Save the DICOM image temporarily
            temp_path = self.storage.save_temp_image(info["pdvs"][0], image_id)

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
