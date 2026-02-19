"""
Study Manager Module

This module provides functionality for managing DICOM studies at a higher level,
including study completion tracking and study-level processing operations.

Classes:
    StudyManager: Manages DICOM studies and their completion status
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

import pydicom

from pixieveil.config import Settings

logger = logging.getLogger(__name__)


class StudyManager:
    """
    Manages DICOM studies and their completion status.
    
    This class provides high-level study management functionality, including:
    - Tracking received images within studies
    - Monitoring study completion based on timeout settings
    - Coordinating study-level processing operations
    - Providing study status information
    
    Attributes:
        settings (Settings): Application configuration settings
        studies (Dict[str, List[Dict]]): Dictionary mapping study UIDs to image information
        study_completion_times (Dict[str, datetime]): Timestamps of last image reception
        completion_timeout (timedelta): Timeout period for study completion
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the StudyManager with application settings.
        
        Args:
            settings: Application configuration settings containing study timeout
                      and other configuration options
        """
        self.settings = settings
        self.studies = defaultdict(list)
        self.study_completion_times = {}
        self.completion_timeout = timedelta(seconds=settings.study.get("completion_timeout", 300))

    async def process_image(self, image_path: Path, image_id: str):
        """
        Process a DICOM image for study management purposes.
        
        This method processes a DICOM image at the study level, updating
        study tracking information and checking for study completion.
        
        Args:
            image_path (Path): Path to the DICOM file to process
            image_id (str): Unique identifier for this DICOM image
            
        Note:
            This method reads the DICOM file to extract study and series
            information, then updates the study tracking system.
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
        Check if a study is complete based on timeout settings.
        
        This method determines if a study has completed receiving images
        by checking if the time since the last image exceeds the configured
        completion timeout.
        
        Args:
            study_uid (str): The StudyInstanceUID to check for completion
            
        Note:
            If a study is determined to be complete, it will be processed
            by the _process_complete_study method.
        """
        try:
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
        
        This method is called when a study is determined to be complete
        (no new images received within the timeout period). It can be
        extended to perform study-level processing operations.
        
        Args:
            study_uid (str): The StudyInstanceUID of the completed study
            
        Note:
            Currently, this method is a placeholder and logs basic information
            about the completed study. Additional study-level processing
            can be implemented here as needed.
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
        Get the current status of a study.
        
        This method provides information about a study's current status,
        including the number of images received and whether the study
        is still active or has been completed.
        
        Args:
            study_uid (str): The StudyInstanceUID to query
            
        Returns:
            Dict[str, Any]: Dictionary containing study status information:
                - study_uid: The StudyInstanceUID
                - status: Current status ('in_progress' or 'not_found')
                - num_images: Number of images received for this study
                - images: List of image IDs received for this study
        """
        study_images = self.studies.get(study_uid, [])
        return {
            "study_uid": study_uid,
            "status": "in_progress" if study_images else "not_found",
            "num_images": len(study_images),
            "images": [image_info["image_id"] for image_info in study_images]
        }
