import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

from pixieveil.config import Settings

logger = logging.getLogger(__name__)

class StudyManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.studies = defaultdict(list)
        self.study_completion_times = {}
        self.completion_timeout = timedelta(seconds=settings.study.get("completion_timeout", 300))

    async def process_image(self, image_path: Path, image_id: str):
        """
        Process a DICOM image for study management.
        """
        try:
            # Read the DICOM image
            ds = pydicom.dcmread(image_path)

            # Get study information
            study_uid = str(ds.StudyInstanceUID)
            series_uid = str(ds.SeriesInstanceUID)

            # Add image to study
            self.studies[study_uid].append({
                "image_id": image_id,
                "series_uid": series_uid,
                "path": image_path,
                "received_at": datetime.now()
            })

            # Update study completion time
            self.study_completion_times[study_uid] = datetime.now()

            # Check if study is complete
            await self._check_study_completion(study_uid)

        except Exception as e:
            logger.error(f"Failed to process study management for image {image_id}: {e}")

    async def _check_study_completion(self, study_uid: str):
        """
        Check if a study is complete and process it.
        """
        try:
            # Get study images
            study_images = self.studies[study_uid]

            # Check if study is complete
            last_received = self.study_completion_times[study_uid]
            if datetime.now() - last_received > self.completion_timeout:
                # Study is complete, process it
                await self._process_complete_study(study_uid)

        except Exception as e:
            logger.error(f"Error checking study completion for {study_uid}: {e}")

    async def _process_complete_study(self, study_uid: str):
        """
        Process a complete study.
        """
        try:
            # Get study images
            study_images = self.studies[study_uid]

            # Process each image in the study
            for image_info in study_images:
                # TODO: Add study-level processing if needed
                pass

            logger.info(f"Study {study_uid} completed with {len(study_images)} images")

        except Exception as e:
            logger.error(f"Error processing complete study {study_uid}: {e}")

    def get_study_status(self, study_uid: str) -> Dict[str, Any]:
        """
        Get the status of a study.
        """
        study_images = self.studies.get(study_uid, [])
        return {
            "study_uid": study_uid,
            "status": "in_progress" if study_images else "not_found",
            "num_images": len(study_images),
            "images": [image_info["image_id"] for image_info in study_images]
        }
