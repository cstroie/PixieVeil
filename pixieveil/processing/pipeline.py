import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings
from pixieveil.processing.anonymizer import Anonymizer
from pixieveil.processing.series_filter import SeriesFilter
from pixieveil.processing.study_manager import StudyManager

logger = logging.getLogger(__name__)

class ProcessingPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.anonymizer = Anonymizer(settings)
        self.series_filter = SeriesFilter(settings)
        self.study_manager = StudyManager(settings)

    async def process_image(self, image_path: Path, image_id: str) -> Optional[Path]:
        """
        Process a received DICOM image through the pipeline.
        """
        try:
            # Read the DICOM image
            ds = pydicom.dcmread(image_path)

            # Validate the image
            if not self._validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                return None

            # Check if image should be filtered
            if self.series_filter.should_filter(ds):
                logger.info(f"Filtering out image {image_id} based on series criteria")
                return None

            # Anonymize the image
            anonymized_path = await self.anonymizer.anonymize(ds, image_path, image_id)
            if not anonymized_path:
                logger.warning(f"Failed to anonymize image: {image_id}")
                return None

            # Process study management
            await self.study_manager.process_image(anonymized_path, image_id)

            logger.info(f"Successfully processed image {image_id}")
            return anonymized_path

        except Exception as e:
            logger.error(f"Failed to process image {image_id}: {e}")
            return None

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
