"""
DICOM Storage Manager Module

This module provides functionality for managing DICOM image storage, processing,
and study completion monitoring.

Classes:
    StorageManager: Manages DICOM image storage and processing operations
"""

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


class StudyState:
    """
    Tracks the state of a DICOM study.
    
    Attributes:
        last_received (float): Timestamp of the last received image for this study
        completed (bool): Flag indicating if the study has been completed and processed
    """
    
    def __init__(self):
        self.last_received = 0.0
        self.completed = False


class StorageManager:
    """
    Manages DICOM image storage, processing, and study completion monitoring.
    
    This class handles the complete lifecycle of DICOM images from temporary storage
    through processing, anonymization, organization into studies/series, and eventual
    archiving and upload to remote storage.
    
    Attributes:
        settings (Settings): Application configuration settings
        base_path (Path): Base directory for storing organized DICOM studies
        anonymizer (Anonymizer): Handler for DICOM anonymization
        series_filter (SeriesFilter): Handler for series filtering
        study_manager (StudyManager): Handler for study management
        study_states (Dict[str, StudyState]): Dictionary tracking study states
        counters (Dict[str, Any]): Dictionary for tracking various counters
    """
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_path = Path(settings.storage.get("base_path", "./storage"))
        self.base_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize processing components
        self.anonymizer = Anonymizer(settings)
        self.series_filter = SeriesFilter(settings)
        self.study_manager = StudyManager(settings)
        
        # Track study states
        self.study_states = {}
        
        # Track counters
        self.counters = {}

    @property
    def completed_count(self) -> int:
        """
        Get the count of completed studies.
        
        Returns:
            int: Number of completed studies
        """
        return sum(1 for study_state in self.study_states.values() if study_state.completed)

    def get_counter(self, category: str, subcategory: str = None, default: Any = 0) -> Any:
        """
        Get a counter value, initializing if necessary.
        
        Args:
            category (str): Category of the counter
            subcategory (str, optional): Subcategory of the counter
            default (Any): Default value if counter doesn't exist
            
        Returns:
            Any: Counter value
        """
        if subcategory:
            key = f"{category}_{subcategory}"
        else:
            key = category
            
        if key not in self.counters:
            self.counters[key] = default
            
        return self.counters[key]

    def save_temp_image(self, pdv: bytes, image_id: str) -> Path:
        """
        Save a DICOM image to temporary storage.
        
        Args:
            pdv (bytes): DICOM pixel data value
            image_id (str): Unique identifier for this DICOM image
            
        Returns:
            Path: Path to the saved temporary DICOM file
        """
        temp_dir = self.base_path / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        temp_file = temp_dir / f"{image_id}.dcm"
        with open(temp_file, "wb") as f:
            f.write(pdv)
            
        return temp_file

    async def process_image(self, image_path: Path, image_id: str):
        """
        Process a received DICOM image through the complete pipeline.
        
        This method orchestrates the complete processing workflow for a DICOM image,
        including validation, filtering, anonymization, and study management.
        
        Args:
            image_path (Path): Path to the DICOM file to process
            image_id (str): Unique identifier for this DICOM image
        """
        try:
            # Read the DICOM image
            ds = pydicom.dcmread(image_path, force=True)

            # Validate the image
            if not self._validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                return

            # Check if image should be filtered
            if self.series_filter.should_filter(ds):
                logger.info(f"Filtering out image {image_id} based on series criteria")
                return

            # Anonymize the image - FIXED: Added missing image_path and image_id arguments
            anonymized_path = self.anonymizer.anonymize(ds, image_path, image_id)
            if not anonymized_path:
                logger.warning(f"Failed to anonymize image: {image_id}")
                return

            # Process study management - FIXED: Added await for async method
            await self.study_manager.process_image(anonymized_path, image_id)

            logger.info(f"Successfully processed image {image_id}")
            
        except Exception as e:
            logger.error(f"Failed to process image {image_id}: {e}")

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
            if not hasattr(ds, field):
                return False

        return True

    async def check_study_completions(self, interval=30):
        """
        Periodically check for completed studies and process them.
        
        Args:
            interval (int): Interval in seconds between checks
        """
        while True:
            await asyncio.sleep(interval)
            
            # Check each study for completion
            for study_uid, study_state in list(self.study_states.items()):
                if not study_state.completed:
                    await self.study_manager._check_study_completion(study_uid)

    def get_counters(self) -> Dict[str, Any]:
        """
        Get all tracked counters.
        
        Returns:
            Dict[str, Any]: Dictionary of all counter values
        """
        return self.counters.copy()
