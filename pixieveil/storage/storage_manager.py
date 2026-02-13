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
    def __init__(self):
        self.last_received = time.time()
        self.completed = False

class StorageManager:
    def __init__(self, settings: Settings):
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
            # Force reading the DICOM image even with missing meta headers
            ds = pydicom.dcmread(image_path, force=True)

            # Validate the image
            if not self._validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                return
                
            # Anonymize the DICOM dataset
            try:
                ds = self.anonymizer.anonymize(ds)
                # Save anonymized version back to temp file with new UIDs
                ds.save_as(image_path, enforce_file_format=False)
            except Exception as e:
                logger.error(f"Failed to anonymize image {image_id}: {e}", exc_info=True)
                return

            # Save original identifiers before anonymization
            original_study_uid = str(ds.StudyInstanceUID)
            
            # Anonymize the DICOM dataset
            try:
                ds = self.anonymizer.anonymize(ds)
                # Save anonymized version back to temp file with new UIDs
                ds.save_as(image_path, enforce_file_format=False)
            except Exception as e:
                logger.error(f"Failed to anonymize image {image_id}: {e}", exc_info=True)
                return

            # Use original study UID for tracking processing state
            study_uid = original_study_uid
            series_uid = str(ds.SeriesInstanceUID)

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
        Validate the DICOM image.
        """
        # Basic validation
        required_fields = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"]
        for field in required_fields:
            if not hasattr(ds, field):
                return False

        return True

    async def check_study_completions(self, interval=10, timeout=60):
        """
        Background task to check for completed studies
        """
        logger.info("Starting study completion checker")
        while True:
            now = time.time()
            logger.debug(f"Checking study completions at {now}")
            logger.debug(f"Tracking {len(self.study_states)} active studies")
            
            if not self.study_states:
                logger.debug("No active studies to check")
                
            for study_uid, state in list(self.study_states.items()):
                logger.debug(f"Study {study_uid} state: {state}")
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
                        zip_path = await self.zip_manager.create_zip(zip_filename, study_dir)
                        if zip_path:
                            # Upload to remote storage
                            success = await self.remote_storage.upload_file(
                                zip_path, 
                                f"studies/{zip_filename}.zip"
                            )
                            if success:
                                logger.info(f"Uploaded study {study_number:04d}")
                                state.completed = True
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
