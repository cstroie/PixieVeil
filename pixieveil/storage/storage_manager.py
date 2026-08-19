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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage
from pixieveil.storage.dicom_storage import DicomStorage
from pixieveil.storage.zip_manager import ZipManager
from pixieveil.processing.anonymizer import Anonymizer
from pixieveil.processing.series_filter import SeriesFilter
from pixieveil.processing.defacer import Defacer
from pixieveil.processing.exam_extractor import ExamExtractor
from pixieveil.processing import exam_merge
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.study_sidecar import StudySidecar

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
        self.retained_path = Path(
            settings.storage.get("retain_path", str(self.base_path.parent / "retained"))
        )
        self.anontrail_path = Path(settings.logging.get("anontrail", "anontrail.jsonl"))
        self.anontrail_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Anonymization mapping trail will be written to: {self.anontrail_path}")

        # Initialize managers
        from pixieveil.processing.study_manager import StudyManager
        self.study_manager = StudyManager(settings)
        self.study_manager.initialize_from_sidecars(self.base_path)
        self.series_filter = SeriesFilter(settings)
        self.anonymizer = Anonymizer(settings)
        self.defacer = Defacer(settings.defacing, temp_path=self.temp_path)
        self.zip_manager = ZipManager(settings)
        self.remote_storage = RemoteStorage(settings)
        self.dicom_storage = DicomStorage(settings)
        self.exam_extractor = ExamExtractor()
        
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
                'dicom_studies': 0,
                'dicom_images': 0,
                'http_studies': 0,
                'http_images': 0,
                'http_bytes': 0,
                'errors': 0
            },
            'performance': {
                'total_time': 0,
                'count_time': 0,
                'average_time': 0
            },
            'defacing': {
                'studies': 0,
                'series_defaced': 0,
                'series_skipped': 0,
                'errors': 0,
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
        self._init_counters_from_sidecars()
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
        # UIDs of studies currently being processed (defaced/zipped/uploaded)
        self._processing_studies: set = set()
        # Study numbers with a manual dashboard action (send/upload/re-extract/
        # save) in progress. Separate from _processing_studies (keyed by
        # study_uid, driven by the automated pipeline) to avoid either set
        # needing to translate between study_number and study_uid to guard
        # the other's operations.
        self._manual_actions_in_progress: set = set()
        # Active pipeline stage counters (thread-safe via self.lock)
        self._active_receiving: int = 0
        self._active_waiting: int = 0
        self._active_defacing: int = 0
        self._active_exporting: int = 0

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
        # start() always sets this before creating the task that runs this
        # coroutine — same reasoning as the assert in stop().
        assert self._stop_event is not None

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
                await self.enforce_retention_purge()
            except Exception as exc:
                logger.error("Unexpected error in retention purge: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def get_counter(self, category: str, subcategory: Optional[str] = None,
                    default: Any = 0) -> Any:
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

    def get_pipeline_status(self) -> dict:
        """Return which pipeline stages are currently active."""
        with self.lock:
            receiving = self._active_receiving > 0
            waiting = self._active_waiting > 0
            defacing = self._active_defacing > 0
            exporting = self._active_exporting > 0
        return {
            "receiving": receiving,
            "waiting": waiting,
            "defacing": defacing,
            "exporting": exporting,
        }

    def set_counter(self, category: str, subcategory: Optional[str] = None,
                    value: Any = 0) -> None:
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

    def inc_counter(self, category: str, subcategory: Optional[str] = None,
                    increment: float = 1) -> None:
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

    def _init_counters_from_sidecars(self) -> None:
        """Seed in-memory counters from the sidecars loaded at startup."""
        sidecars = self.study_manager._sidecars
        if not sidecars:
            return

        total_series = 0
        total_images = 0
        archived_studies = 0

        for study_uid, sc in sidecars.items():
            total_series += len(sc.series)
            for rec in sc.series.values():
                key = (sc.study_number, rec.series_number)
                total_images += self.study_manager.image_counters.get(key, 0)
            if sc.status == "archived":
                archived_studies += 1

        self.counters['storage']['studies'] = len(sidecars)
        self.counters['storage']['series'] = total_series
        self.counters['storage']['images'] = total_images
        self.counters['archive']['studies'] = archived_studies

        logger.info(
            "Counters restored from sidecars: %d studies, %d series, %d images, %d archived",
            len(sidecars), total_series, total_images, archived_studies,
        )

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

        with self.lock:
            self._active_receiving += 1
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

            # Update storage counters and persist sidecar for new study/series
            if is_new_series:
                base_path = self.base_path
                study_dir = base_path / f"{study_number:04d}"
                if not study_dir.exists():
                    with self.lock:
                        self.inc_counter('storage', 'studies')
                with self.lock:
                    self.inc_counter('storage', 'series')
                logger.debug(f"Creating new series {series_number} for study {study_number}")

                # anonymize() has already run on this ds, so the study/series
                # UID mappings are guaranteed present (apply_uid_mapping caches
                # "" rather than None for an unmapped strategy — never a bare
                # None). Patient ID is different: get_patient_id_mapping only
                # ever gets populated for a PSEUDO/PSEUDOUID PatientID
                # strategy, so a profile using CLEAR/KEEP/a literal genuinely
                # has no entry — fall back to "" the same way UID mapping does,
                # rather than writing None into the sidecar.
                anon_study_uid = self.anonymizer.get_study_uid_mapping(study_uid)
                anon_series_uid = self.anonymizer.get_series_uid_mapping(series_uid)
                assert anon_study_uid is not None
                assert anon_series_uid is not None
                anon_patient_id = self.anonymizer.get_patient_id_mapping(patient_id) or ""
                self.study_manager.record_new_series(
                    study_uid, series_uid, patient_id,
                    anon_study_uid, anon_series_uid, anon_patient_id,
                    study_number, series_number,
                )

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
        finally:
            with self.lock:
                self._active_receiving -= 1

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
        Detect completed studies and launch each one as an independent background
        task so the event loop is never blocked by defacing, ZIP creation, or upload.
        """
        completed_study_uids = self.study_manager.check_study_completions()
        if not completed_study_uids:
            return

        logger.info("Found %d completed studies to process", len(completed_study_uids))

        for study_uid in completed_study_uids:
            if study_uid in self._processing_studies:
                logger.debug("Study %s already being processed — skipping", study_uid)
                continue
            self._processing_studies.add(study_uid)
            self._active_waiting += 1
            asyncio.create_task(self._process_study(study_uid))

    async def _process_study(self, study_uid: str) -> None:
        """
        Process one completed study end-to-end in a background task.

        Blocking work (defacing, ZIP creation, file cleanup) is offloaded to a
        thread via asyncio.to_thread so the event loop stays responsive.
        """
        try:
            with self.lock:
                self._active_waiting -= 1

            study_number = self.study_manager.get_study_number(study_uid)
            if not study_number:
                logger.warning("No study number found for completed study %s", study_uid)
                return

            study_dir = self.base_path / f"{study_number:04d}"
            if not study_dir.exists():
                logger.warning("Study directory missing for %s: %s", study_uid, study_dir)
                with self.lock:
                    self.inc_counter('errors', 'total')
                self.study_manager.mark_study_archived(study_uid, via=None)
                return

            logger.info("Processing completed study: %04d (%s)", study_number, study_uid)

            # RHYTHM exam extraction only needs anonymized headers, which are
            # already final at this point — defacing only ever rewrites
            # PixelData, never metadata. Doing this first means the exam
            # sidecar survives a defacing crash/timeout, and doesn't sit
            # behind however long CPU-mode nnU-Net inference takes. Skipped
            # if a sidecar already exists so a re-queued retry never clobbers
            # paper-note data merged in by hand afterward.
            if not ExamSidecar.path_for(self.base_path, study_number).exists():
                try:
                    await asyncio.to_thread(self._write_exam_sidecar, study_uid, study_number, study_dir)
                except Exception:
                    logger.exception("Failed to extract RHYTHM exam data for study %04d", study_number)

            # Defacing — skip if this study already finished processing
            # (e.g. re-queued spuriously after reaching "ready"/"archived").
            sc = self.study_manager._sidecars.get(study_uid)
            already_processed = sc is not None and sc.status in ("ready", "archived")
            if self.defacer.enabled and not already_processed:
                with self.lock:
                    self._active_defacing += 1
                try:
                    await asyncio.to_thread(self._deface_study, study_uid, study_number, study_dir)
                finally:
                    with self.lock:
                        self._active_defacing -= 1

            # Count images (blocking glob — run in thread)
            image_count = await asyncio.to_thread(
                lambda: sum(
                    1 for f in study_dir.rglob("*.dcm")
                    if not any(part.endswith("_pre_deface") for part in f.parts)
                )
            )
            logger.debug("Study %04d contains %d images", study_number, image_count)

            with self.lock:
                self.inc_counter('archive', 'studies')
                self.inc_counter('archive', 'images', image_count)

            # Exports are never triggered automatically — the study sits in
            # base_path until a user sends/uploads it from the /studies
            # dashboard (StorageManager.manual_send_dicom / manual_upload_http).
            self.study_manager.mark_study_ready(study_uid)
        except Exception:
            logger.exception("Unexpected error processing study %s", study_uid)
        finally:
            self._processing_studies.discard(study_uid)

    def _write_exam_sidecar(self, study_uid: str, study_number: int, study_dir: Path) -> None:
        """
        Blocking: derive RHYTHM exam-entry fields from study_dir's DICOM
        headers and write them to <study_number>_exam.json. Must run before
        export/retention can move or delete study_dir.
        """
        sc = self.study_manager._sidecars.get(study_uid)
        anonymized_study_uid = sc.anonymized_study_uid if sc is not None else ""
        data = self.exam_extractor.extract(study_dir, study_number, anonymized_study_uid)
        ExamSidecar(study_number, data).save(self.base_path)

    def _deface_study(self, study_uid: str, study_number: int, study_dir: Path) -> None:
        """
        Blocking: deface all head-scan series in one study.
        Called via asyncio.to_thread — must not use await.
        """
        self.study_manager.mark_study_defacing(study_uid)
        self.inc_counter('defacing', 'studies')
        for series_dir in sorted(study_dir.iterdir()):
            if not series_dir.is_dir():
                continue
            if series_dir.name.endswith("_pre_deface"):
                continue

            series_number = int(series_dir.name) if series_dir.name.isdigit() else None
            orig_series_uid = (
                self.study_manager.get_original_series_uid(study_uid, series_number)
                if series_number is not None else None
            )

            if orig_series_uid and self.study_manager.is_series_defaced(study_uid, orig_series_uid):
                logger.info("Series %s already defaced — skipping", series_dir.name)
                continue

            if self.defacer.is_topogram(series_dir):
                logger.info("Series %s is a topogram/scout — skipping defacing", series_dir.name)
                self.inc_counter('defacing', 'series_skipped')
                if orig_series_uid:
                    self.study_manager.set_series_classification(
                        study_uid, orig_series_uid, is_head=False, is_topogram=True
                    )
            elif self.defacer.is_head_scan(series_dir):
                if orig_series_uid:
                    self.study_manager.set_series_classification(
                        study_uid, orig_series_uid, is_head=True, is_topogram=False
                    )
                logger.info("Defacing series %s in study %04d", series_dir.name, study_number)
                ok = self.defacer.deface_series(series_dir, data_dir=self.base_path)
                if ok:
                    self.inc_counter('defacing', 'series_defaced')
                    if orig_series_uid:
                        self.study_manager.mark_series_defaced(study_uid, orig_series_uid)
                else:
                    self.inc_counter('defacing', 'errors')
            else:
                logger.debug("Series %s is not a head scan — skipping defacing", series_dir.name)
                self.inc_counter('defacing', 'series_skipped')
                if orig_series_uid:
                    self.study_manager.set_series_classification(
                        study_uid, orig_series_uid, is_head=False, is_topogram=False
                    )
    
    def _retain_or_delete_sync(self, study_dir: Path) -> None:
        """
        Called after a successful export in place of an unconditional delete.

        If storage.retain_after_export_days is configured, moves the
        (already anonymized) study directory into retained_path with a
        timestamp marker instead of deleting it, so enforce_retention_purge
        can remove it once the window elapses. Otherwise deletes it right
        away, matching the original always-delete behavior.
        """
        retain_days = self.settings.storage.get("retain_after_export_days")
        if not retain_days:
            shutil.rmtree(study_dir)
            return

        self.retained_path.mkdir(parents=True, exist_ok=True)
        dest = self.retained_path / study_dir.name
        if dest.exists():
            dest = self.retained_path / f"{study_dir.name}_{int(time.time())}"
        shutil.move(str(study_dir), str(dest))
        (dest / ".retained_at").write_text(str(time.time()))
        logger.debug("Retention: kept %s under %s", study_dir.name, self.retained_path)

    def enforce_retention_purge_sync(self) -> None:
        """
        Synchronous purge of retained studies whose retention window has
        elapsed. No-op if storage.retain_after_export_days is not configured.
        """
        retain_days = self.settings.storage.get("retain_after_export_days")
        if not retain_days or not self.retained_path.exists():
            return

        cutoff = time.time() - (retain_days * 86400)
        for study_dir in self.retained_path.iterdir():
            if not study_dir.is_dir():
                continue
            marker = study_dir / ".retained_at"
            try:
                retained_at = float(marker.read_text()) if marker.exists() else study_dir.stat().st_mtime
            except (OSError, ValueError):
                retained_at = study_dir.stat().st_mtime
            if retained_at < cutoff:
                shutil.rmtree(study_dir, ignore_errors=True)
                logger.info(
                    "Retention: purged %s (older than retain_after_export_days=%s)",
                    study_dir.name, retain_days,
                )

    async def enforce_retention_purge(self) -> None:
        """Offload retention purge to a thread so the event loop stays responsive."""
        await asyncio.to_thread(self.enforce_retention_purge_sync)

    def enforce_storage_quota_sync(self) -> None:
        """
        Synchronous quota enforcement. Removes the oldest archived studies from
        base_path (lowest study number first) until disk usage drops below 75% of
        the configured max_storage_gb limit. Active (in-progress) studies, and
        studies that are "ready" but have never been exported, are never
        touched — export is manual-only now, so purging an un-exported study
        would destroy data nobody ever got a chance to send anywhere.
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

        skipped_unexported = 0
        for study_dir in study_dirs:
            if used_bytes <= target_bytes:
                break

            study_number = int(study_dir.name)
            if study_number in active_study_numbers:
                continue

            sc = self.study_manager.get_sidecar_by_number(study_number)
            if sc is None or sc.status != "archived":
                skipped_unexported += 1
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

        if used_bytes > target_bytes and skipped_unexported:
            logger.warning(
                "Storage quota still exceeded after cleanup (%.2f GB used, limit %s GB) — "
                "%d study(ies) awaiting manual export were left in place. Export or "
                "manually clear them from the /studies dashboard to free space.",
                used_bytes / (1024 ** 3), max_storage_gb, skipped_unexported,
            )

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

    # -----------------------------------------------------------------
    # Manual dashboard actions (/studies page)
    #
    # Exports are user-triggered only — StorageManager never sends a study
    # to a DICOM node or HTTP endpoint on its own. The first successful
    # manual send/upload for a study also performs the same
    # retain-or-delete + mark_study_archived bookkeeping _export_via_dicom /
    # _export_via_http_zip used to do automatically. A later manual re-send
    # of an already-archived study must NOT move or delete files that are
    # already in their resting place, so that step is skipped then.
    # -----------------------------------------------------------------

    def resolve_study_dir(self, study_number: int) -> tuple[Optional[Path], str]:
        """Locate a study's directory, checking base_path then retained_path."""
        base_dir = self.base_path / f"{study_number:04d}"
        if base_dir.exists():
            return base_dir, "base"
        retained_dir = self.retained_path / f"{study_number:04d}"
        if retained_dir.exists():
            return retained_dir, "retained"
        return None, "absent"

    def find_sidecar_by_number(self, study_number: int) -> Optional[StudySidecar]:
        return self.study_manager.get_sidecar_by_number(study_number)

    async def manual_send_dicom(self, study_number: int) -> dict:
        """Send a study to the configured DICOM node at the user's request."""
        if study_number in self._manual_actions_in_progress:
            return {"ok": False, "message": "action already in progress for this study"}
        self._manual_actions_in_progress.add(study_number)
        with self.lock:
            self._active_exporting += 1
        try:
            study_dir, location = self.resolve_study_dir(study_number)
            if study_dir is None:
                return {"ok": False, "message": "study directory not found"}
            if not self.dicom_storage.enabled:
                return {"ok": False, "message": "DICOM export is not configured"}

            sc = self.find_sidecar_by_number(study_number)
            first_export = sc is not None and sc.status != "archived"

            success = await self.dicom_storage.send_study(study_dir)
            if success:
                image_count = await asyncio.to_thread(
                    lambda: sum(
                        1 for f in study_dir.rglob("*.dcm")
                        if not any(part.endswith("_pre_deface") for part in f.parts)
                    )
                )
                with self.lock:
                    self.inc_counter('export', 'dicom_studies')
                    self.inc_counter('export', 'dicom_images', image_count)
                if first_export:
                    assert sc is not None  # implied by first_export being True
                    with self.lock:
                        self.inc_counter('cleanup', 'studies')
                        self.inc_counter('cleanup', 'images', image_count)
                    await asyncio.to_thread(self._retain_or_delete_sync, study_dir)
                    self.study_manager.mark_study_archived(sc.original_study_uid, via="dicom")
                return {"ok": True, "message": f"sent to DICOM node ({location})"}

            with self.lock:
                self.inc_counter('export', 'errors')
                self.inc_counter('errors', 'total')
            return {"ok": False, "message": "DICOM send failed"}
        finally:
            self._manual_actions_in_progress.discard(study_number)
            with self.lock:
                self._active_exporting -= 1

    async def manual_upload_http(self, study_number: int) -> dict:
        """Zip and upload a study to the configured HTTP endpoint at the user's request."""
        if study_number in self._manual_actions_in_progress:
            return {"ok": False, "message": "action already in progress for this study"}
        self._manual_actions_in_progress.add(study_number)
        with self.lock:
            self._active_exporting += 1
        try:
            study_dir, location = self.resolve_study_dir(study_number)
            if study_dir is None:
                return {"ok": False, "message": "study directory not found"}
            if not self.remote_storage.enabled:
                return {"ok": False, "message": "HTTP export is not configured"}

            sc = self.find_sidecar_by_number(study_number)
            first_export = sc is not None and sc.status != "archived"

            zip_path = await self.zip_manager.create_zip(
                f"{study_number:04d}", self.base_path, source_dir=study_dir
            )
            if not zip_path:
                with self.lock:
                    self.inc_counter('archive', 'errors')
                    self.inc_counter('errors', 'total')
                return {"ok": False, "message": "failed to create ZIP archive"}

            zip_size = await asyncio.to_thread(lambda: zip_path.stat().st_size)
            try:
                success = await self.remote_storage.upload_file(zip_path, zip_path.name)
            finally:
                await asyncio.to_thread(zip_path.unlink, missing_ok=True)

            if success:
                image_count = await asyncio.to_thread(
                    lambda: sum(
                        1 for f in study_dir.rglob("*.dcm")
                        if not any(part.endswith("_pre_deface") for part in f.parts)
                    )
                )
                with self.lock:
                    self.inc_counter('export', 'http_studies')
                    self.inc_counter('export', 'http_images', image_count)
                    self.inc_counter('export', 'http_bytes', zip_size)
                if first_export:
                    assert sc is not None  # implied by first_export being True
                    with self.lock:
                        self.inc_counter('cleanup', 'studies')
                        self.inc_counter('cleanup', 'images', image_count)
                    await asyncio.to_thread(self._retain_or_delete_sync, study_dir)
                    self.study_manager.mark_study_archived(sc.original_study_uid, via="http")
                return {"ok": True, "message": f"uploaded to HTTP storage ({location})"}

            with self.lock:
                self.inc_counter('export', 'errors')
                self.inc_counter('errors', 'total')
            return {"ok": False, "message": "HTTP upload failed"}
        finally:
            self._manual_actions_in_progress.discard(study_number)
            with self.lock:
                self._active_exporting -= 1

    def _manual_reextract_sync(self, study_number: int, study_dir: Path) -> dict:
        exam_path = ExamSidecar.path_for(self.base_path, study_number)
        previous: dict = {}
        if exam_path.exists():
            try:
                previous = ExamSidecar.load(exam_path).data
            except Exception:
                logger.warning(
                    "Could not read existing exam sidecar %s — proceeding without it",
                    exam_path, exc_info=True,
                )

        sc = self.find_sidecar_by_number(study_number)
        anonymized_study_uid = sc.anonymized_study_uid if sc is not None else ""

        fresh = self.exam_extractor.extract(study_dir, study_number, anonymized_study_uid)
        merged = exam_merge.merge_extracted(fresh, previous)
        ExamSidecar(study_number, merged).save(self.base_path)
        return merged

    async def manual_reextract(self, study_number: int) -> dict:
        """Re-run RHYTHM exam extraction, preserving manual overlay fields."""
        if study_number in self._manual_actions_in_progress:
            return {"ok": False, "message": "action already in progress for this study"}
        self._manual_actions_in_progress.add(study_number)
        try:
            study_dir, _location = self.resolve_study_dir(study_number)
            if study_dir is None:
                return {"ok": False, "message": "study directory not found"}
            try:
                merged = await asyncio.to_thread(
                    self._manual_reextract_sync, study_number, study_dir
                )
            except Exception:
                logger.exception("Manual re-extract failed for study %04d", study_number)
                return {"ok": False, "message": "re-extraction failed"}
            return {"ok": True, "data": merged}
        finally:
            self._manual_actions_in_progress.discard(study_number)

    def _save_exam_sync(self, study_number: int, manual_fields: dict) -> Optional[dict]:
        exam_path = ExamSidecar.path_for(self.base_path, study_number)
        if not exam_path.exists():
            return None

        sidecar = ExamSidecar.load(exam_path)
        data = sidecar.data
        existing_manual = data.get("manual") or {"edited_at": None, "fields": {}}
        merged_fields = {**existing_manual.get("fields", {}), **manual_fields}
        manual = {
            "edited_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds"),
            "fields": merged_fields,
        }
        exam_merge.apply_manual(data, manual)
        exam_merge.recompute_buckets(data)
        ExamSidecar(study_number, data).save(self.base_path)

        # Weight/age corrections are worth pushing into the DICOM headers
        # themselves, not just the exam sidecar — otherwise the correction
        # never reaches whatever the study eventually gets exported as.
        # Only safe while the files are still sitting in base_path awaiting
        # manual export; once archived, exported copies are already gone.
        def _as_number(v: Any) -> Optional[float]:
            # Guards against a malformed PUT body (string/list/dict/bool)
            # reaching round()/pydicom below and crashing the request after
            # the sidecar write above already succeeded.
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        weight_kg = _as_number(manual_fields.get("patient.weight_kg"))
        age_years = _as_number(manual_fields.get("patient.age_years"))
        if weight_kg is not None or age_years is not None:
            sc = self.find_sidecar_by_number(study_number)
            if sc is not None and sc.status != "archived":
                study_dir, location = self.resolve_study_dir(study_number)
                if study_dir is not None and location == "base":
                    self._writeback_patient_fields_sync(study_dir, weight_kg, age_years)

        return data

    def _writeback_patient_fields_sync(self, study_dir: Path,
                                        weight_kg: Optional[float],
                                        age_years: Optional[float]) -> None:
        """Patch a manually-corrected PatientWeight/PatientAge into every
        anonymized DICOM file under study_dir, so the correction reaches
        the objects that eventually get exported.

        Untested edge case this guards against: DICOM's AS VR for
        PatientAge is a fixed 4 characters ("034Y"). A negative or >=1000
        age_years would format as "-05Y" or "1500Y" — neither is valid AS
        content, and a downstream DICOM reader could reject the whole
        object over one bad tag. Weight has no VR-length constraint at
        plausible values but is range-checked too, since it's the same
        "operator fat-fingered the form" failure mode.
        """
        if weight_kg is not None and not (0 < weight_kg <= 500):
            logger.warning(
                "Refusing to write back implausible PatientWeight=%s kg into %s",
                weight_kg, study_dir,
            )
            weight_kg = None
        if age_years is not None and not (0 <= age_years < 1000):
            logger.warning(
                "Refusing to write back implausible PatientAge=%s years into %s",
                age_years, study_dir,
            )
            age_years = None
        if weight_kg is None and age_years is None:
            return

        age_str = f"{int(round(age_years)):03d}Y" if age_years is not None else None

        for dcm_path in study_dir.rglob("*.dcm"):
            if any(part.endswith("_pre_deface") for part in dcm_path.parts):
                continue
            try:
                ds = pydicom.dcmread(dcm_path)
                if weight_kg is not None:
                    ds.PatientWeight = weight_kg
                if age_str is not None:
                    ds.PatientAge = age_str
                ds.save_as(dcm_path)
            except Exception:
                logger.exception("Failed to write back patient fields into %s", dcm_path)

    async def save_exam(self, study_number: int, manual_fields: dict) -> dict:
        """Apply manually-entered fields to a study's exam sidecar and save it."""
        if study_number in self._manual_actions_in_progress:
            return {"ok": False, "message": "action already in progress for this study"}
        self._manual_actions_in_progress.add(study_number)
        try:
            data = await asyncio.to_thread(self._save_exam_sync, study_number, manual_fields)
            if data is None:
                return {"ok": False, "message": "exam sidecar not found"}
            return {"ok": True, "data": data}
        finally:
            self._manual_actions_in_progress.discard(study_number)
