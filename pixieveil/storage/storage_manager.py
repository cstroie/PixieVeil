"""
Storage Manager Module

This module provides functionality for managing DICOM image storage, including:
- Temporary storage of received DICOM images
- Processing and organizing images into studies and series
- Anonymization of DICOM data
- Background monitoring of study completion
- ZIP creation and remote storage upload

Classes:
    StudyState: Tracks the state of a DICOM study
    StorageManager: Main class for managing DICOM image storage and processing
"""

import asyncio
import logging
import shutil
import tempfile
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage
from pixieveil.storage.zip_manager import ZipManager
from pixieveil.dashboard.sse import image_counter
from pixieveil.processing.anonymizer import Anonymizer

logger = logging.getLogger(__name__)


class StudyState:
    """
    Tracks the state of a DICOM study.
    
    Attributes:
        last_received (float): Timestamp of the last received image for this study
        completed (bool): Flag indicating if the study has been completed and processed
    """
    
    def __init__(self):
        """
        Initialize a new StudyState instance.
        """
        self.last_received = time.time()
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
        temp_path (Path): Temporary directory for storing incoming DICOM images
        remote_storage (RemoteStorage): Handler for remote storage operations
        zip_manager (ZipManager): Handler for ZIP archive creation
        anonymizer (Anonymizer): Handler for DICOM anonymization
        study_states (Dict[str, StudyState]): Dictionary tracking active studies
        completed_count (int): Counter for completed studies
        study_counter (int): Counter for assigning numeric study IDs
        study_map (Dict[str, int]): Maps StudyInstanceUID to numeric study number
        series_map (Dict[tuple, tuple]): Maps (StudyUID, SeriesUID) to (study_num, series_num)
        image_counters (Dict[tuple, int]): Tracks image numbers within each series
        _lock (threading.Lock): Thread lock for thread-safe operations
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the StorageManager with application settings.
        
        Args:
            settings: Application configuration settings containing storage paths
                      and other configuration options
        """
        self.settings = settings
        self.base_path = Path(settings.storage["base_path"])
        self.temp_path = Path(settings.storage["temp_path"])
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.temp_path.mkdir(parents=True, exist_ok=True)
        self.remote_storage = RemoteStorage(settings)
        self.zip_manager = ZipManager(settings)
        self.anonymizer = Anonymizer(settings)
        self.study_states = {}  # study_uid: StudyState
        self.completed_count = 0
        
        # Numbering counters
        # Find the latest used study number from existing directories
        existing_studies = [d.name for d in self.base_path.iterdir() if d.is_dir()]
        study_numbers = []
        for name in existing_studies:
            if len(name) == 4 and name.isdigit():
                study_numbers.append(int(name))
        
        self.study_counter = max(study_numbers) if study_numbers else 0
        self.study_map = {}  # StudyInstanceUID -> study_number
        self.series_map = {}  # (StudyInstanceUID, SeriesInstanceUID) -> (study_number, series_number)
        self.image_counters = {}  # (study_number, series_number) -> image_counter
        self._lock = threading.Lock()

    def save_temp_image(self, pdv: bytes, image_id: str) -> Path:
        """
        Save a received DICOM image to temporary storage.
        
        This method saves incoming DICOM data to a temporary file for later processing.
        The image is saved with a unique ID to prevent conflicts.
        
        Args:
            pdv (bytes): DICOM pixel data received from the DICOM server
            image_id (str): Unique identifier for this DICOM image
            
        Returns:
            Path: Path to the saved temporary DICOM file
            
        Raises:
            OSError: If the file cannot be written to temporary storage
        """
        temp_file = self.temp_path / f"{image_id}.dcm"
        with open(temp_file, "wb") as f:
            f.write(pdv)

        return temp_file

    def process_image(self, image_path: Path, image_id: str):
        """
        Process a received DICOM image through the complete pipeline.
        
        This method handles the complete processing of a DICOM image:
        1. Reads and validates the DICOM dataset
        2. Anonymizes the DICOM data
        3. Organizes the image into appropriate study/series structure
        4. Assigns numeric identifiers and filenames
        5. Moves the image to its final organized location
        6. Updates study tracking and counters
        
        Args:
            image_path (Path): Path to the temporary DICOM file to process
            image_id (str): Unique identifier for this DICOM image
            
        Raises:
            Exception: If any step in the processing pipeline fails
        """
        try:
            # Force reading the DICOM image even with missing meta headers
            ds = pydicom.dcmread(image_path, force=True)

            # Validate the image
            if not self._validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                return

            # Save original identifiers before anonymization
            study_uid = str(ds.StudyInstanceUID)
            series_uid = str(ds.SeriesInstanceUID)
            
            # Anonymize the DICOM dataset
            try:
                ds = self.anonymizer.anonymize(ds)
                # Save anonymized version back to temp file with new UIDs
                ds.save_as(image_path, enforce_file_format=False)
            except Exception as e:
                logger.error(f"Failed to anonymize image {image_id}: {e}", exc_info=True)
                return

            with self._lock:
                # Assign study number
                if study_uid not in self.study_map:
                    self.study_counter += 1
                    self.study_map[study_uid] = self.study_counter
                
                study_number = self.study_map[study_uid]
            
                # Assign series number
                key = (study_uid, series_uid)
                if key not in self.series_map:
                    # Find highest existing series number for this study
                    study_dir = self.base_path / f"{study_number:04d}"
                    if study_dir.exists():
                        existing_series = [d.name for d in study_dir.iterdir() if d.is_dir()]
                        series_numbers = [int(name) for name in existing_series if len(name) == 4 and name.isdigit()]
                        series_count = max(series_numbers) + 1 if series_numbers else 1
                    else:
                        series_count = 1
                    
                    self.series_map[key] = (study_number, series_count)
                
                study_number, series_number = self.series_map[key]
                
                # Get next image number in series
                if (study_number, series_number) not in self.image_counters:
                    self.image_counters[(study_number, series_number)] = 0
                self.image_counters[(study_number, series_number)] += 1
                image_number = self.image_counters[(study_number, series_number)]

            # Create numeric paths (4-digit padded)
            study_dir = self.base_path / f"{study_number:04d}"
            series_dir = study_dir / f"{series_number:04d}"
            study_dir.mkdir(exist_ok=True)
            series_dir.mkdir(exist_ok=True)

            # Save image with numeric filename
            image_dest = series_dir / f"{image_number:04d}.dcm"
            shutil.move(image_path, image_dest)
            
            # Update received image counter and study state
            image_counter.increment()
            
            # Thread-safe update of study_states
            with self._lock:
                # Only create new StudyState if it doesn't exist
                if study_uid not in self.study_states:
                    self.study_states[study_uid] = StudyState()
                else:
                    # Update last received time for existing study
                    self.study_states[study_uid].last_received = time.time()

            logger.info(f"Processed image {image_id} for study {study_uid}")

        except Exception as e:
            logger.error(f"Failed to process image {image_id}: {e}", exc_info=True)

    def _validate_dicom(self, ds: pydicom.Dataset) -> bool:
        """
        Validate the DICOM image for required fields and basic integrity.
        
        This method checks if the DICOM dataset contains all required fields
        for proper processing and storage.
        
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
        Background task to check for completed studies and process them.
        
        This method runs continuously in the background, checking if any active
        studies have timed out (no new images received within the configured
        timeout period). When a study is detected as complete, it:
        1. Creates a ZIP archive of the study
        2. Uploads the archive to remote storage (if configured)
        3. Cleans up local files
        4. Updates study tracking
        
        Args:
            interval (int): Check interval in seconds (default: 30)
            
        Note:
            This method is designed to be run as an asyncio background task
            and will continue running until cancelled.
        """
        # Get completion timeout from settings, default to 120 seconds if not specified
        timeout = self.settings.study.get("completion_timeout", 120)
        
        logger.info(f"Starting study completion checker with timeout: {timeout}s")
        while True:
            now = time.time()
            logger.debug(f"Checking study completions at {now}")
            
            # Thread-safe access to study_states
            with self._lock:
                study_states_copy = dict(self.study_states)
            
            if study_states_copy:
                logger.debug(f"Tracking {len(self.study_states)} active studies")
            else:
                logger.debug("No active studies to check")
                
            for study_uid, state in list(study_states_copy.items()):
                if not state.completed and (now - state.last_received) > timeout:
                    # Process completed study
                    # Get numeric study ID from mapping
                    study_number = self.study_map.get(study_uid)
                    if not study_number:
                        logger.warning(f"No study number found for {study_uid}")
                        continue
                    
                    study_dir = self.base_path / f"{study_number:04d}"
                    if study_dir.exists():
                        logger.info(f"Processing completed study: {study_number:04d} ({study_uid})")
                        
                        # Create ZIP archive
                        zip_filename = f"{study_number:04d}"
                        zip_path = await self.zip_manager.create_zip(zip_filename, self.base_path)
                        if zip_path:
                            # Upload to remote storage
                            success = await self.remote_storage.upload_file(
                                zip_path, 
                                f"studies/{zip_filename}.zip"
                            )
                            # If remote not configured
                            if success is None:
                                # Thread-safe update of study_states
                                with self._lock:
                                    if study_uid in self.study_states:
                                        self.study_states[study_uid].completed = True
                                        self.completed_count += 1
                                        # Clean up files
                                        del self.study_states[study_uid]
                            elif success:
                                logger.info(f"Uploaded study {study_number:04d}")
                                # Thread-safe update of study_states and file cleanup
                                with self._lock:
                                    if study_uid in self.study_states:
                                        self.study_states[study_uid].completed = True
                                        self.completed_count += 1
                                        # Clean up files
                                        shutil.rmtree(study_dir)
                                        zip_path.unlink()
                                        del self.study_states[study_uid]
                            else:
                                logger.error(f"Failed to upload study {study_uid}")
                        else:
                            logger.error(f"Failed to create ZIP for study {study_uid}")
                    else:
                        logger.warning(f"Study directory missing for {study_uid}")
            
            await asyncio.sleep(interval)
