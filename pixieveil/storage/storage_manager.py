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
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage
from pixieveil.storage.zip_manager import ZipManager
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
        counters (Dict[str, int]): Dictionary for tracking various statistics
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the StorageManager with application settings.
        
        Args:
            settings: Application configuration settings containing storage paths
                      and other configuration options
        """
        logger.debug("Initializing StorageManager...")
        self.settings = settings
        self.base_path = Path(settings.storage["base_path"])
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Created base directory: {self.base_path}")
        self.temp_path = Path(settings.storage["temp_path"])
        self.temp_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Created temp directory: {self.temp_path}")
        
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
        logger.debug(f"Found existing studies: {existing_studies}")
        logger.debug(f"Starting study counter from: {self.study_counter}")
        
        self.study_map = {}  # StudyInstanceUID -> study_number
        self.series_map = {}  # (StudyInstanceUID, SeriesInstanceUID) -> (study_number, series_number)
        self.image_counters = {}  # (study_number, series_number) -> image_counter
        self._lock = threading.Lock()
        
        # Statistics counters
        self.counters = {
            'reception': {
                'studies': 0,
                'images': 0,
                'bytes': 0
            },
            'processing': {
                'studies': 0,
                'images': 0,
                'anonymized_images': 0,
                'errors': {
                    'anonymization': 0,
                    'validation': 0,
                    'processing': 0
                }
            },
            'storage': {
                'studies': 0,
                'series': 0,
                'images': 0
            },
            'archive': {
                'studies': 0,
                'images': 0,
                'errors': 0
            },
            'export': {
                'studies': 0,
                'images': 0,
                'errors': 0
            },
            'remote_storage': {
                'studies': 0,
                'images': 0,
                'errors': 0,
                'bytes': 0
            },
            'performance': {
                'total_time': 0,
                'count_time': 0,
                'average_time': 0
            },
            'cleanup': {
                'studies': 0,
                'images': 0
            },
            'errors': {
                'total': 0,
                'reconnection_attempts': 0,
                'timeout_errors': 0
            }
        }
        logger.debug("StorageManager initialization complete")

        # -----------------------------------------------------------------
        # Background‑task handling for study‑completion checking
        # -----------------------------------------------------------------
        # These attributes are created here so that type‑checkers (mypy,
        # pyright) know they exist, but they are initialised to ``None``.
        # ``start()`` will create the task; ``stop()`` will cancel it.
        self._completion_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._shutting_down = False

    # -----------------------------------------------------------------
    # Public lifecycle helpers
    # -----------------------------------------------------------------
    async def start(self) -> None:
        """
        Launch the background coroutine that periodically checks for
        completed studies. The coroutine runs until :meth:`stop` is called.
        This method is idempotent – calling it multiple times will only
        create a single task.
        """
        if self._completion_task is not None:
            logger.debug("StorageManager.start() called but task already running")
            return

        logger.info("Starting StorageManager background study‑completion checker")
        self._stop_event = asyncio.Event()
        self._completion_task = asyncio.create_task(self._completion_loop())

    async def stop(self) -> None:
        """
        Gracefully stop the background completion‑check task.
        """
        if self._completion_task is None:
            logger.debug("StorageManager.stop() called but no task is running")
            return

        logger.info("Stopping StorageManager background study‑completion checker")
        self._shutting_down = True
        assert self._stop_event is not None
        self._stop_event.set()
        self._completion_task.cancel()
        try:
            await self._completion_task
        except asyncio.CancelledError:
            pass
        finally:
            self._completion_task = None
            self._stop_event = None

    # -----------------------------------------------------------------
    # Internal helper that drives ``check_study_completions`` in a loop
    # -----------------------------------------------------------------
    async def _completion_loop(self) -> None:
        """
        Re‑run :meth:`check_study_completions` at the interval defined in the
        configuration (default 30 s). The heavy‑weight ZIP creation is executed
        in a thread‑pool so the event‑loop stays responsive.
        """
        interval = self.settings.study.get("completion_check_interval", 30)

        while not self._stop_event.is_set():
            try:
                await self.check_study_completions(interval)
            except Exception as exc:  # pragma: no‑cover
                logger.error("Unexpected error in study‑completion loop: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def get_counter(self, category: str, subcategory: str = None, default: Any = 0) -> Any:
        """
        Get a counter value from the hierarchical counters structure.
        
        This method provides a safe way to access nested counter values
        without repetitive nested dictionary access patterns.
        
        Args:
            category (str): Top-level category name (e.g., 'reception', 'processing')
            subcategory (str, optional): Subcategory name (e.g., 'images', 'errors')
            default (Any, optional): Default value to return if counter not found
            
        Returns:
            Any: The counter value or default if not found
            
        Example:
            # Get reception images count
            images = self.get_counter('reception', 'images')
            
            # Get validation errors
            validation_errors = self.get_counter('processing', 'errors', 'validation')
            
            # Get top-level counter
            total_errors = self.get_counter('errors', 'total')
        """
        with self._lock:
            if category not in self.counters:
                return default
            
            if subcategory is None:
                return self.counters[category]
            
            if subcategory in self.counters[category]:
                return self.counters[category][subcategory]
            
            # If subcategory is not found but we're looking for nested errors
            if subcategory == 'errors' and 'errors' in self.counters[category]:
                if isinstance(self.counters[category]['errors'], dict):
                    return self.counters[category]['errors']
            
            return default

    def save_temp_image(self, ds: pydicom.Dataset, image_id: str) -> Path:
        """
        Save a received DICOM image to temporary storage.
        
        This method saves incoming DICOM data to a temporary file for later processing.
        The image is saved with a unique ID to prevent conflicts.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to save
            image_id (str): Unique identifier for this DICOM image
            
        Returns:
            Path: Path to the saved temporary DICOM file
            
        Raises:
            OSError: If the file cannot be written to temporary storage
        """
        logger.debug(f"Saving temporary image {image_id}")
        temp_file = self.temp_path / f"{image_id}.dcm"
        with open(temp_file, "wb") as f:
            ds.save_as(f, enforce_file_format=True)

        # Update reception counters
        with self._lock:
            self.counters['reception']['images'] += 1
            self.counters['reception']['bytes'] += temp_file.stat().st_size
            
            # Check if this is the first image for a new study
            # Note: We can't determine study UID until we read the DICOM file
            # This will be updated in process_image method

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
        # Skip processing if shutting down
        if self._shutting_down:
            logger.debug(f"Skipping image {image_id} during shutdown")
            return
            
        logger.debug(f"Starting processing of image {image_id} from {image_path}")
        start_time = time.time()
        study_uid = None
        
        try:
            # Force reading the DICOM image even with missing meta headers
            logger.debug(f"Reading DICOM file: {image_path}")
            ds = pydicom.dcmread(image_path, force=True)

            # Validate the image
            logger.debug(f"Validating DICOM image {image_id}")
            if not self._validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                with self._lock:
                    self.counters['processing']['errors']['validation'] += 1
                    self.counters['errors']['total'] += 1
                return

            # Save original identifiers before anonymization
            study_uid = str(ds.StudyInstanceUID)
            series_uid = str(ds.SeriesInstanceUID)
            logger.debug(f"Image {image_id} belongs to study {study_uid}, series {series_uid}")
            
            # Update reception counters for new studies
            with self._lock:
                if study_uid not in self.study_map:
                    self.counters['reception']['studies'] += 1
                    logger.debug(f"New study detected: {study_uid}")
            
            # Anonymize the DICOM dataset
            logger.debug(f"Starting anonymization of image {image_id}")
            try:
                ds = self.anonymizer.anonymize(ds)
                # Save anonymized version back to temp file with new UIDs
                ds.save_as(image_path, enforce_file_format=False)
                with self._lock:
                    self.counters['processing']['anonymized_images'] += 1
                logger.debug(f"Successfully anonymized image {image_id}")
            except Exception as e:
                logger.error(f"Failed to anonymize image {image_id}: {e}", exc_info=True)
                with self._lock:
                    self.counters['processing']['errors']['anonymization'] += 1
                    self.counters['errors']['total'] += 1
                return

            with self._lock:
                # Assign study number
                if study_uid not in self.study_map:
                    self.study_counter += 1
                    self.study_map[study_uid] = self.study_counter
                    logger.debug(f"Assigned new study number {self.study_counter} to study {study_uid}")
                
                # Get assigned study number for this study UID
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
                        logger.debug(f"Study {study_number} has existing series: {existing_series}")
                    else:
                        series_count = 1
                        logger.debug(f"Creating new series {series_count} for study {study_number}")
                        self.counters['storage']['studies'] += 1
                        self.counters['storage']['series'] += 1
                    
                    self.series_map[key] = (study_number, series_count)
                    logger.debug(f"Assigned new series number {series_count} to series {series_uid}")

                # Get assigned study and series numbers for this image
                study_number, series_number = self.series_map[key]
                
                # Get next image number in series
                if (study_number, series_number) not in self.image_counters:
                    self.image_counters[(study_number, series_number)] = 0
                    logger.debug(f"Starting new image counter for study {study_number}, series {series_number}")
                self.image_counters[(study_number, series_number)] += 1
                image_number = self.image_counters[(study_number, series_number)]
                logger.debug(f"Image {image_id} will be saved as image number {image_number} in series {series_number}")

            # Create numeric paths (4-digit padded)
            study_dir = self.base_path / f"{study_number:04d}"
            series_dir = study_dir / f"{series_number:04d}"
            study_dir.mkdir(exist_ok=True)
            series_dir.mkdir(exist_ok=True)
            logger.debug(f"Created directories: study {study_dir}, series {series_dir}")

            # Save image with numeric filename
            image_dest = series_dir / f"{image_number:04d}.dcm"
            logger.debug(f"Moving image from {image_path} to {image_dest}")
            shutil.move(image_path, image_dest)
            
            # Thread-safe update of study_states and storage counters
            with self._lock:
                # Only create new StudyState if it doesn't exist
                if study_uid not in self.study_states:
                    self.study_states[study_uid] = StudyState()
                    logger.debug(f"Created new StudyState for study {study_uid}")
                else:
                    # Update last received time for existing study
                    self.study_states[study_uid].last_received = time.time()
                    logger.debug(f"Updated last received time for study {study_uid}")
                
                # Update storage counters
                self.counters['storage']['images'] += 1
                self.counters['processing']['images'] += 1
                self.counters['processing']['studies'] = len(self.study_map)
                logger.debug(f"Updated storage counters: storage_images={self.counters['storage']['images']}, processing_studies={self.counters['processing']['studies']}")

            # Update processing time
            processing_time = time.time() - start_time
            with self._lock:
                self.counters['performance']['total_time'] += processing_time
                self.counters['performance']['count_time'] += 1
                self.counters['performance']['average_time'] = (
                    self.counters['performance']['total_time'] / self.counters['performance']['count_time']
                )
            logger.debug(f"Image {image_id} processed in {processing_time:.3f}s")

            logger.info(f"Processed image {image_id} for study {study_uid}")

        except Exception as e:
            logger.error(f"Failed to process image {image_id}: {e}", exc_info=True)
            with self._lock:
                self.counters['processing']['errors']['processing'] += 1
                self.counters['errors']['total'] += 1

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
        logger.debug("Validating DICOM dataset")
        # Basic validation
        required_fields = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"]
        for field in required_fields:
            if not hasattr(ds, field):
                logger.warning(f"Missing required field: {field}")
                return False

        logger.debug("DICOM validation passed")
        return True

    async def check_study_completions(self, interval=30):
        """
        Check for completed studies and process them.

        This method performs a single pass over active studies, identifying any
        that have timed out (no new images received within the configured
        timeout period). For each timed‑out study it:
        1. Creates a ZIP archive of the study
        2. Uploads the archive to remote storage (if configured)
        3. Cleans up local files
        4. Updates study tracking counters

        Args:
            interval (int): Check interval in seconds (default: 30).  The
                caller (the background loop) is responsible for sleeping
                between calls.
        """
        # Get completion timeout from settings, default to 120 seconds if not specified
        timeout = self.settings.study.get("completion_timeout", 120)
        logger.info(f"Running study completion check (timeout: {timeout}s)")

        now = time.time()

        # Thread‑safe snapshot of current study states
        with self._lock:
            study_states_copy = dict(self.study_states)

        if study_states_copy:
            logger.debug(f"Tracking {len(self.study_states)} active studies")
            for study_uid, state in self.study_states.items():
                time_since_last = now - state.last_received
                logger.debug(f"Study {study_uid}: last received {time_since_last:.1f}s ago")

        for study_uid, state in list(study_states_copy.items()):
            if not state.completed and (now - state.last_received) > timeout:
                logger.info(f"Study {study_uid} timed out ({now - state.last_received:.1f}s since last image)")
                # Process completed study
                study_number = self.study_map.get(study_uid)
                if not study_number:
                    logger.warning(f"No study number found for {study_uid}")
                    continue

                study_dir = self.base_path / f"{study_number:04d}"
                if not study_dir.exists():
                    logger.warning(f"Study directory missing for {study_uid}")
                    with self._lock:
                        self.counters['errors']['total'] += 1
                    continue

                logger.info(f"Processing completed study: {study_number:04d} ({study_uid})")
                image_count = sum(len(list(study_dir.rglob("*.dcm"))) for _ in [None])
                logger.debug(f"Study {study_number:04d} contains {image_count} images")

                # Update archive counters
                with self._lock:
                    self.counters['archive']['studies'] += 1
                    self.counters['archive']['images'] += image_count

                # Create ZIP archive
                zip_filename = f"{study_number:04d}"
                logger.debug(f"Creating ZIP archive for study {zip_filename}")
                zip_path = await self.zip_manager.create_zip(zip_filename, self.base_path)
                if not zip_path:
                    logger.error(f"Failed to create ZIP for study {study_uid}")
                    with self._lock:
                        self.counters['archive']['errors'] += 1
                        self.counters['errors']['total'] += 1
                    continue

                logger.info(f"Created ZIP archive: {zip_path}")

                # Update export counters
                with self._lock:
                    self.counters['export']['studies'] += 1
                    self.counters['export']['images'] += image_count

                # Upload to remote storage
                logger.debug(f"Uploading study {zip_path} to remote storage")
                success = await self.remote_storage.upload_file(zip_path, f"{zip_path}.zip")

                if success is None:
                    logger.info(f"Remote storage not configured, keeping local files for study {zip_filename}")
                    with self._lock:
                        if study_uid in self.study_states:
                            self.study_states[study_uid].completed = True
                            self.completed_count += 1
                            self.counters['cleanup']['studies'] += 1
                            self.counters['cleanup']['images'] += image_count
                            del self.study_states[study_uid]
                elif success:
                    logger.info(f"Successfully uploaded study {study_number:04d}")
                    with self._lock:
                        if study_uid in self.study_states:
                            self.study_states[study_uid].completed = True
                            self.completed_count += 1
                            self.counters['remote_storage']['studies'] += 1
                            self.counters['remote_storage']['images'] += image_count
                            self.counters['remote_storage']['bytes'] += zip_path.stat().st_size
                            self.counters['cleanup']['studies'] += 1
                            self.counters['cleanup']['images'] += image_count
                            logger.debug(f"Cleaning up study directory: {study_dir}")
                            shutil.rmtree(study_dir)
                            zip_path.unlink()
                            del self.study_states[study_uid]
                else:
                    logger.error(f"Failed to upload study {study_uid}")
                    with self._lock:
                        self.counters['remote_storage']['errors'] += 1
                        self.counters['archive']['errors'] += 1
                        self.counters['errors']['total'] += 1
    
    def get_counters(self) -> Dict[str, Any]:
        """
        Get all current counters and statistics.
        
        Returns:
            Dict[str, Any]: Dictionary containing all current counter values
        """
        logger.debug("Retrieving storage counters")
        with self._lock:
            return dict(self.counters)
