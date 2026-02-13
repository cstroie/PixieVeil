import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage
from pixieveil.storage.zip_manager import ZipManager
from pixieveil.dashboard.sse import image_counter

logger = logging.getLogger(__name__)

class StorageManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_path = Path(settings.storage["base_path"])
        self.temp_path = Path(settings.storage["temp_path"])
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.temp_path.mkdir(parents=True, exist_ok=True)
        self.remote_storage = RemoteStorage(settings)
        self.zip_manager = ZipManager(settings)

    def save_temp_image(self, pdv: bytes, image_id: str) -> Path:
        """
        Save a received DICOM image to temporary storage.
        """
        temp_file = self.temp_path / f"{image_id}.dcm"
        with open(temp_file, "wb") as f:
            f.write(pdv)

        return temp_file

    def process_image(self, image_path: Path, image_id: str):
        """
        Process a received DICOM image.
        """
        try:
            # Read the DICOM image
            ds = pydicom.dcmread(image_path)

            # Validate the image
            if not self._validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                return

            # Get study information
            study_uid = str(ds.StudyInstanceUID)
            series_uid = str(ds.SeriesInstanceUID)

            # Create study directory if it doesn't exist
            study_dir = self.base_path / study_uid
            study_dir.mkdir(exist_ok=True)

            # Save the image to study directory
            image_dest = study_dir / f"{series_uid}_{image_id}.dcm"
            shutil.move(image_path, image_dest)
            
            # Update received image counter
            image_counter.increment()

            logger.info(f"Processed image {image_id} for study {study_uid}")

        except Exception as e:
            logger.error(f"Failed to process image {image_id}: {e}", exc_info=True)

    def _validate_dicom(self, ds: pydicom.Dataset) -> bool:
        """
        Validate the DICOM image.
        """
        # Basic validation
        required_fields = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"]
        for field in required_fields:
            if not hasattr(ds, field):
                return False

        return True
