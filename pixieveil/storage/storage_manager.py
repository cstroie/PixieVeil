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
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage
from pixieveil.storage.zip_manager import ZipManager
from pixieveil.processing.anonymizer import Anonymizer
from pixieveil.processing.study_manager import StudyManager, StudyState
from pixieveil.processing.series_filter import SeriesFilter
from pixieveil.processing.defacer import Defacer

logger = logging.getLogger(__name__)


class StorageManager:
    """
    Manages DICOM image storage and processing workflow.
    
    This class handles the complete lifecycle of DICOM images from temporary storage
    through processing, anonymization, organization, and eventual archiving.
    It coordinates with multiple specialized managers:
    - StudyManager: Handles study/series numbering and completion tracking
    - Anonymizer: Handles DICOM anonymization
    - SeriesFilter: Filters series based on criteria
    - ZipManager: Creates study archives
    - RemoteStorage: Uploads to remote systems
    
    Attributes:
        settings (Settings): Application configuration settings
        base_path (Path): Base directory for storing organized DICOM studies
        temp_path (Path): Temporary directory for storing incoming DICOM images
        study_manager (StudyManager): Manager for study lifecycle
        series_filter (SeriesFilter): Filters series based on criteria
        anonymizer (Anonymizer): Handler for DICOM anonymization
        zip_manager (ZipManager): Handler for ZIP archive creation
        remote_storage (RemoteStorage): Handler for remote storage operations
        anontrail_path (Path): Path to audit log for anonymization mappings
        lock (threading.Lock): Thread lock for thread-safe operations
        counters (Dict[str, Any]): Dictionary for tracking various statistics
        _completion_task (Optional[asyncio.Task]): Background task for completion checking
        _stop_event (Optional[asyncio.Event]): Event to signal background task to stop
        _shutting_down (bool): Flag indicating shutdown in progress
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
        self.anontrail_path = Path(settings.logging.get("anontrail", "anontrail.jsonl"))
        self.anontrail_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Anonymization mapping trail will be written to: {self.anontrail_path}")

        # Initialize managers
        self.study_manager = StudyManager(settings)
        self.study_manager.initialize_from_existing_studies(self.base_path)
        self.series_filter = SeriesFilter(settings)
        self.anonymizer = Anonymizer(settings)
        self.defacer = Defacer(settings.defacing, temp_path=self.temp_path)
        self.zip_manager = ZipManager(settings)
        self.remote_storage = RemoteStorage(settings)
        
        # Thread safety
        self.lock = threading.Lock()

        # Per-study accumulated byte counts for new studies received this session.
        # Existing studies (loaded from disk on startup) are not tracked here.
        self.study_size_bytes: Dict[str, int] = {}
        
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
                'filtered_images': 0,
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
        self._completion_task = asyncio.create_task(self.completion_loop())

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
    async def completion_loop(self) -> None:
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
                await self.enforce_storage_quota()
            except Exception as exc:
                logger.error("Unexpected error in storage quota enforcement: %s", exc)

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
        with self.lock:
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

    def set_counter(self, category: str, subcategory: str = None, value: Any = 0) -> None:
        """
        Set a counter to a specific value in the hierarchical counters structure.
        
        This method provides a thread-safe way to set counter values at any level
        of the counters hierarchy. It will create intermediate dictionaries if needed.
        
        Args:
            category (str): Top-level category name (e.g., 'reception', 'processing')
            subcategory (str, optional): Subcategory name (e.g., 'images', 'errors')
            value (Any, optional): Value to set the counter to (default: 0)
            
        Example:
            # Set reception images count
            storage_manager.set_counter('reception', 'images', 100)
            
            # Set a top-level counter
            storage_manager.set_counter('errors', 'total', 5)
            
            # Set a nested error counter
            storage_manager.set_counter('processing', 'errors', 'validation', 3)
        """
        if category not in self.counters:
            self.counters[category] = {}
        
        if subcategory is None:
            # Set the entire category to the value (usually a dict)
            self.counters[category] = value
        else:
            # Ensure the category is a dictionary
            if not isinstance(self.counters[category], dict):
                self.counters[category] = {}
            
            self.counters[category][subcategory] = value

    def inc_counter(self, category: str, subcategory: str = None, increment: int = 1) -> None:
        """
        Increment a counter by a specified value in the hierarchical counters structure.
        
        This method provides a thread-safe way to increment counter values. If the
        counter or any intermediate structure doesn't exist, it will be created with
        an initial value of 0 before incrementing.
        
        Args:
            category (str): Top-level category name (e.g., 'reception', 'processing')
            subcategory (str, optional): Subcategory name (e.g., 'images', 'errors')
            increment (int, optional): Value to add to the counter (default: 1)
            
        Example:
            # Increment reception images count by 1
            storage_manager.inc_counter('reception', 'images')
            
            # Increment by a specific amount
            storage_manager.inc_counter('processing', 'images', 5)
            
            # Increment a nested error counter
            storage_manager.inc_counter('processing', 'errors', 'validation', 1)
        """
        if category not in self.counters:
            self.counters[category] = {}
        
        if subcategory is None:
            # Increment the category itself (should be a number)
            if not isinstance(self.counters[category], (int, float)):
                self.counters[category] = 0
            self.counters[category] += increment
        else:
            # Ensure the category is a dictionary
            if not isinstance(self.counters[category], dict):
                self.counters[category] = {}
            
            if subcategory not in self.counters[category]:
                self.counters[category][subcategory] = 0
            self.counters[category][subcategory] += increment

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
        with self.lock:
            self.inc_counter('reception', 'images')
            self.inc_counter('reception', 'bytes', temp_file.stat().st_size)
            
            # Check if this is the first image for a new study
            # Note: We can't determine study UID until we read the DICOM file
            # This will be updated in process_image method

        return temp_file

    def log_anonymization_mapping(self, original_study_uid: str, original_series_uid: str, 
                                  original_patient_id: str, image_id: str, 
                                  anonymized_study_number: int, anonymized_series_number: int):
        """
        Log the anonymization mapping to audit trail file.
        
        Writes a JSON line for each anonymized image containing the mapping information.
        This is useful for tracing back anonymized data to original records if needed.
        
        Args:
            original_study_uid (str): Original Study Instance UID
            original_series_uid (str): Original Series Instance UID
            original_patient_id (str): Original Patient ID
            image_id (str): Unique image identifier
            anonymized_study_number (int): Assigned numeric study number
            anonymized_series_number (int): Assigned numeric series number
        """
        try:
            # Get the anonymized UIDs from the anonymizer
            anon_study_uid = self.anonymizer.get_study_uid_mapping(original_study_uid)
            anon_series_uid = self.anonymizer.get_series_uid_mapping(original_series_uid)
            anon_patient_id = self.anonymizer.get_patient_id_mapping(original_patient_id)
            
            mapping_record = {
                'timestamp': datetime.now().isoformat(),
                'image_id': image_id,
                'original': {
                    'study_uid': original_study_uid,
                    'series_uid': original_series_uid,
                    'patient_id': original_patient_id
                },
                'anonymized': {
                    'study_uid': anon_study_uid,
                    'series_uid': anon_series_uid,
                    'patient_id': anon_patient_id,
                    'study_number': str(anonymized_study_number).zfill(4),
                    'series_number': str(anonymized_series_number).zfill(4)
                }
            }
            
            # Append to JSONL file (JSON Lines format - one JSON object per line)
            with self.lock:
                with open(self.anontrail_path, 'a') as f:
                    f.write(json.dumps(mapping_record) + '\n')
            
            logger.debug(f"Logged anonymization mapping for image {image_id}")
        except Exception as e:
            logger.error(f"Failed to log anonymization mapping for image {image_id}: {e}", exc_info=True)

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
            if not self.validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                with self.lock:
                    self.counters['processing']['errors']['validation'] += 1
                    self.inc_counter('errors', 'total')
                return

            # Save original identifiers before anonymization
            study_uid = str(ds.StudyInstanceUID)
            series_uid = str(ds.SeriesInstanceUID)
            patient_id = str(ds.PatientID) if "PatientID" in ds else "UNKNOWN"
            logger.debug(f"Image {image_id} belongs to study {study_uid}, series {series_uid}, patient {patient_id}")
            
            # Check if image should be filtered based on series criteria
            if self.series_filter.should_filter(ds):
                logger.info(f"Filtering out image {image_id} based on series criteria")
                image_path.unlink(missing_ok=True)
                with self.lock:
                    self.inc_counter('processing', 'filtered_images')
                return
            
            # Update reception counters for new studies; start tracking their size
            image_size = image_path.stat().st_size
            study_number = self.study_manager.get_study_number(study_uid)
            if study_number is None:
                with self.lock:
                    self.inc_counter('reception', 'studies')
                    self.inc_counter('processing', 'studies')
                    self.study_size_bytes[study_uid] = 0
                logger.debug(f"New study detected: {study_uid}")

            # Enforce per-study size limit (only for studies tracked this session)
            max_study_size_mb = self.settings.study.get("max_study_size_mb")
            if max_study_size_mb and study_uid in self.study_size_bytes:
                with self.lock:
                    accumulated = self.study_size_bytes[study_uid]
                if accumulated + image_size > max_study_size_mb * 1024 * 1024:
                    study_number_known = self.study_manager.get_study_number(study_uid) or "NEW"
                    logger.warning(
                        f"Study {study_number_known} has reached the size limit "
                        f"({(accumulated + image_size) / (1024 * 1024):.1f} MB > {max_study_size_mb} MB), "
                        f"dropping image {image_id}"
                    )
                    image_path.unlink(missing_ok=True)
                    with self.lock:
                        self.inc_counter('processing', 'filtered_images')
                    return

            # Anonymize the DICOM dataset
            logger.debug(f"Starting anonymization of image {image_id}")
            try:
                ds = self.anonymizer.anonymize(ds)
                # Save anonymized version back to temp file with new UIDs
                ds.save_as(image_path, enforce_file_format=False)
                with self.lock:
                    self.inc_counter('processing', 'anonymized_images')
                logger.debug(f"Successfully anonymized image {image_id}")
            except Exception as e:
                logger.error(f"Failed to anonymize image {image_id}: {e}", exc_info=True)
                with self.lock:
                    self.counters['processing']['errors']['anonymization'] += 1
                    self.inc_counter('errors', 'total')
                return

            # Assign study/series/image numbers using StudyManager
            study_number, series_number, image_number, is_new_series = self.study_manager.add_image_to_study(study_uid, series_uid)

            # Update storage counters for new series
            if is_new_series:
                base_path = self.base_path
                study_dir = base_path / f"{study_number:04d}"
                if not study_dir.exists():
                    with self.lock:
                        self.inc_counter('storage', 'studies')
                with self.lock:
                    self.inc_counter('storage', 'series')
                logger.debug(f"Creating new series {series_number} for study {study_number}")
            
            # Log the anonymization mapping after study/series numbers are assigned
            self.log_anonymization_mapping(study_uid, series_uid, patient_id, image_id, 
                                            study_number, series_number)

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

            # Accumulate size for this study's session tracker
            if study_uid in self.study_size_bytes:
                with self.lock:
                    self.study_size_bytes[study_uid] += image_size

            # Update storage counters
            with self.lock:
                self.inc_counter('storage', 'images')
                self.inc_counter('processing', 'images')
                logger.debug(f"Updated storage counters: storage_images={self.counters['storage']['images']}, active_studies={self.study_manager.get_active_study_count()}")

            # Update processing time
            processing_time = time.time() - start_time
            with self.lock:
                self.inc_counter('performance', 'total_time', processing_time)
                self.inc_counter('performance', 'count_time')
                self.set_counter('performance', 'average_time', 
                    self.counters['performance']['total_time'] / self.counters['performance']['count_time']
                )
            logger.debug(f"Image {image_id} processed in {processing_time:.3f}s")

            logger.info(f"Processed image {image_id} for study {study_uid}")

        except Exception as e:
            logger.error(f"Failed to process image {image_id}: {e}", exc_info=True)
            with self.lock:
                self.counters['processing']['errors']['processing'] += 1
                self.inc_counter('errors', 'total')

    def validate_dicom(self, ds: pydicom.Dataset) -> bool:
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
        # Use StudyManager to check for completed studies
        completed_study_uids = self.study_manager.check_study_completions()
        
        if not completed_study_uids:
            return
        
        logger.info(f"Found {len(completed_study_uids)} completed studies to process")
        
        for study_uid in completed_study_uids:
            # Get study number from StudyManager
            study_number = self.study_manager.get_study_number(study_uid)
            if not study_number:
                logger.warning(f"No study number found for completed study {study_uid}")
                continue

            study_dir = self.base_path / f"{study_number:04d}"
            if not study_dir.exists():
                logger.warning(f"Study directory missing for {study_uid}: {study_dir}")
                with self.lock:
                    self.inc_counter('errors', 'total')
                self.study_manager.mark_study_archived(study_uid)
                continue

            logger.info(f"Processing completed study: {study_number:04d} ({study_uid})")

            # Deface head-scan series before archiving
            if self.defacer.enabled:
                for series_dir in sorted(study_dir.iterdir()):
                    if not series_dir.is_dir():
                        continue
                    if self.defacer.is_head_scan(series_dir):
                        logger.info(f"Defacing series {series_dir.name} in study {study_number:04d}")
                        self.defacer.deface_series(series_dir)
                    else:
                        logger.debug(f"Series {series_dir.name} is not a head scan, skipping defacing")

            image_count = sum(
                1 for f in study_dir.rglob("*.dcm")
                if not any(part.endswith("_pre_deface") for part in f.parts)
            )
            logger.debug(f"Study {study_number:04d} contains {image_count} images")

            # Update archive counters
            with self.lock:
                self.inc_counter('archive', 'studies')
                self.inc_counter('archive', 'images', image_count)

            # Create ZIP archive
            zip_filename = f"{study_number:04d}"
            logger.debug(f"Creating ZIP archive for study {zip_filename}")
            zip_path = await self.zip_manager.create_zip(zip_filename, self.base_path)
            if not zip_path:
                logger.error(f"Failed to create ZIP for study {study_uid}")
                with self.lock:
                    self.inc_counter('archive', 'errors')
                    self.inc_counter('errors', 'total')
                continue

            logger.info(f"Created ZIP archive: {zip_path}")

            # Update export counters
            with self.lock:
                self.inc_counter('export', 'studies')
                self.inc_counter('export', 'images', image_count)

            # Upload to remote storage
            logger.debug(f"Uploading study {zip_path} to remote storage")
            success = await self.remote_storage.upload_file(zip_path, zip_path.name)

            if success is None:
                logger.info(f"Remote storage not configured, keeping local files for study {zip_filename}")
                with self.lock:
                    self.inc_counter('cleanup', 'studies')
                    self.inc_counter('cleanup', 'images', image_count)
                self.study_manager.mark_study_archived(study_uid)
            elif success:
                logger.info(f"Successfully uploaded study {study_number:04d}")
                with self.lock:
                    self.inc_counter('remote_storage', 'studies')
                    self.inc_counter('remote_storage', 'images', image_count)
                    self.inc_counter('remote_storage', 'bytes', zip_path.stat().st_size)
                    self.inc_counter('cleanup', 'studies')
                    self.inc_counter('cleanup', 'images', image_count)
                logger.debug(f"Cleaning up study directory: {study_dir}")
                shutil.rmtree(study_dir)
                zip_path.unlink()
                self.study_manager.mark_study_archived(study_uid)
            else:
                logger.error(f"Failed to upload study {study_uid}")
                with self.lock:
                    self.inc_counter('remote_storage', 'errors')
                    self.inc_counter('archive', 'errors')
                    self.inc_counter('errors', 'total')
    
    def enforce_storage_quota_sync(self) -> None:
        """
        Synchronous quota enforcement. Removes the oldest completed studies from
        base_path (lowest study number first) until disk usage drops below 75% of
        the configured max_storage_gb limit.  Active (in-progress) studies are
        never touched.
        """
        max_storage_gb = self.settings.storage.get("max_storage_gb")
        if not max_storage_gb:
            return

        max_bytes = int(max_storage_gb * 1024 * 1024 * 1024)
        target_bytes = int(max_bytes * 0.75)

        used_bytes = sum(f.stat().st_size for f in self.base_path.rglob("*") if f.is_file())
        if used_bytes <= max_bytes:
            return

        logger.warning(
            f"Storage quota exceeded: {used_bytes / (1024 ** 3):.2f} GB used, "
            f"limit is {max_storage_gb} GB. Removing oldest studies..."
        )

        active_study_numbers = self.study_manager.get_active_study_numbers()

        # Collect 4-digit study directories, sorted oldest first
        study_dirs = sorted(
            [d for d in self.base_path.iterdir()
             if d.is_dir() and len(d.name) == 4 and d.name.isdigit()],
            key=lambda d: int(d.name)
        )

        for study_dir in study_dirs:
            if used_bytes <= target_bytes:
                break

            study_number = int(study_dir.name)
            if study_number in active_study_numbers:
                continue

            dir_size = sum(f.stat().st_size for f in study_dir.rglob("*") if f.is_file())
            shutil.rmtree(study_dir)
            used_bytes -= dir_size
            logger.info(f"Quota: removed study {study_dir.name} ({dir_size / (1024 ** 2):.1f} MB)")

            zip_path = self.base_path / f"{study_dir.name}.zip"
            if zip_path.exists():
                zip_size = zip_path.stat().st_size
                zip_path.unlink()
                used_bytes -= zip_size

            with self.lock:
                self.inc_counter('cleanup', 'studies')

        logger.info(f"Storage after quota cleanup: {used_bytes / (1024 ** 3):.2f} GB")

    async def enforce_storage_quota(self) -> None:
        """Offload quota enforcement to a thread so the event loop stays responsive."""
        await asyncio.to_thread(self.enforce_storage_quota_sync)

    def get_counters(self) -> Dict[str, Any]:
        """
        Get all current counters and statistics.

        Returns:
            Dict[str, Any]: Dictionary containing all current counter values
        """
        logger.debug("Retrieving storage counters")
        with self.lock:
            return dict(self.counters)
