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
import os
import shutil
import time
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import pydicom
from pydicom.uid import (
    CTImageStorage,
    EnhancedCTImageStorage,
    LegacyConvertedEnhancedCTImageStorage,
)

from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage
from pixieveil.storage.dicom_storage import DicomStorage
from pixieveil.storage.rhythm_storage import RhythmStorage, sha256_file
from pixieveil.events import EventBus
from pixieveil.storage.zip_manager import ZipManager
from pixieveil.processing.anonymizer import Anonymizer
from pixieveil.processing.series_filter import SeriesFilter
from pixieveil.processing.defacer import Defacer, DefacingCancelled
from pixieveil.processing.exam_extractor import ExamExtractor
from pixieveil.processing import exam_merge
from pixieveil.processing import rhythm_manifest
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.study_sidecar import StudySidecar, _now_iso

logger = logging.getLogger(__name__)


def _format_dicom_age(age_years: float) -> str:
    """Format years as a DICOM AS value in completed units (truncated, as
    ages are counted): days under a month, months under 2 years, else
    years. Whole years would turn a 0.6-year infant into "001Y" — a
    different RHYTHM age group — and every neonate into "000Y"."""
    eps = 1e-6  # 0.58333… × 12 must count as 7 months, not 6
    if age_years >= 2:
        return f"{int(age_years + eps):03d}Y"
    months = int(age_years * 12 + eps)
    if months >= 1:
        return f"{months:03d}M"
    return f"{int(age_years * 365.25 + eps):03d}D"


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
        # Holds <NNNN>.zip + <NNNN>.json for HTTP/RHYTHM exports — written
        # before the upload, kept as the retry source / audit copy. Purged
        # together with the study directory once the retention window
        # elapses (see enforce_retention_purge_sync).
        self.export_path = Path(
            settings.storage.get("export_path", str(self.base_path.parent / "export"))
        )
        self.anontrail_path = Path(settings.logging.get("anontrail", "anontrail.jsonl"))
        self.anontrail_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Anonymization mapping trail will be written to: {self.anontrail_path}")

        # Initialize managers
        from pixieveil.processing.study_manager import StudyManager
        self.study_manager = StudyManager(settings)
        self.study_manager.initialize_from_sidecars(self.base_path)
        # Live dashboard updates (SSE). Thin notifications only — the page
        # re-fetches through the regular endpoints. No-op until start() binds
        # it to the running loop, so tests/CLI paths are unaffected.
        self.events = EventBus()
        self.study_manager.on_change = lambda sc: self.events.publish(
            "study", n=sc.study_number, status=sc.status)
        self._recv_counts: dict[int, int] = {}
        self.series_filter = SeriesFilter(settings)
        self.anonymizer = Anonymizer(settings)
        self.defacer = Defacer(settings.defacing, temp_path=self.temp_path)
        self.zip_manager = ZipManager(settings)
        self.remote_storage = RemoteStorage(settings)
        self.dicom_storage = DicomStorage(settings)
        self.rhythm_storage = RhythmStorage(settings)
        # One scanners & protocols catalog shared by RHYTHM export, exam
        # extraction and the dashboard's /protocols page.
        self.protocol_catalog = self.rhythm_storage.protocol_map
        self.exam_extractor = ExamExtractor(catalog=self.protocol_catalog)
        
        # Thread safety
        self.lock = threading.Lock()

        # Per-study accumulated byte counts for new studies received this session.
        # Existing studies (loaded from disk on startup) are not tracked here.
        self.study_size_bytes: Dict[str, int] = {}

        # Per-study pre-anonymization extraction hints (Manufacturer / model /
        # ProtocolName / StudyDescription / per-series SeriesDescription),
        # captured from each raw image before anonymize() strips them. Kept in
        # memory as the most complete view and flushed to
        # <study_dir>/.exam_hints.json once per series, so ExamExtractor can
        # still resolve the scanner/indication after the study completes even
        # under the 'rhythm' profile. "first non-empty wins" per field.
        self._exam_hints: Dict[str, Dict[str, Any]] = {}
        
        # Statistics counters. Dict[str, Any], not something more precise
        # like Dict[str, Dict[str, Any]] or a TypedDict per category —
        # get_counter/set_counter/inc_counter (and a few call sites that
        # index self.counters directly) are all called with arbitrary
        # runtime strings for category/subcategory, not a fixed literal set
        # a TypedDict could check. Most categories hold a Dict[str, int|
        # float] of subcategories, but inc_counter/set_counter also support
        # a bare category with no subcategory (a plain int/float value) —
        # that duality is why the category slot itself has to be Any rather
        # than a nested dict type.
        self.counters: Dict[str, Any] = {
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
                'rhythm_studies': 0,
                'rhythm_images': 0,
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
        self._sweep_orphan_deface_temp_sync()
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
        # study_number -> monotonic time of the last RHYTHM study-id lookup.
        self._rhythm_id_attempts: dict[int, float] = {}
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
        Defacer.reset_cancel()
        if self._completion_task is not None:
            logger.debug("StorageManager.start() called but task already running")
            return

        logger.info("Starting StorageManager background study‑completion checker")
        self.events.bind(asyncio.get_running_loop())
        self._stop_event = asyncio.Event()
        self._completion_task = asyncio.create_task(self.completion_loop())

    async def stop(self) -> None:
        """
        Gracefully stop the background completion‑check task.
        """
        # Defacing runs nnU-Net in a child process; stop it now so shutdown
        # doesn't wait minutes for inference (the study resumes on restart).
        Defacer.cancel_all()
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
                await self.reconcile_rhythm_jobs()
            except Exception as exc:
                logger.error("Unexpected error in RHYTHM job reconciliation: %s", exc)

            try:
                await self.reapply_catalog_if_changed()
            except Exception as exc:
                logger.error("Unexpected error re-matching the protocol catalog: %s", exc)

            try:
                await self.prepare_ready_exports()
            except Exception as exc:
                logger.error("Unexpected error preparing export artifacts: %s", exc)

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
        events = getattr(self, "events", None)  # inc_counter may run mid-__init__
        if events is not None:
            events.counters()

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
                # Owner-only: the trail maps pseudonyms back to real identities.
                fd = os.open(self.anontrail_path,
                             os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'a') as f:
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

            # Capture the handful of non-PHI technical fields ExamExtractor
            # needs for the scanner/indication lookup *before* anonymize()
            # runs — the 'rhythm' profile strips Manufacturer / model /
            # ProtocolName / StudyDescription / SeriesDescription outright, so
            # by the time extraction reads the completed study they're gone.
            pre_anon_hints = {
                "manufacturer": str(getattr(ds, "Manufacturer", "") or "").strip(),
                "model": str(getattr(ds, "ManufacturerModelName", "") or "").strip(),
                "protocol_name": str(getattr(ds, "ProtocolName", "") or "").strip(),
                "study_description": str(getattr(ds, "StudyDescription", "") or "").strip(),
                "series_description": str(getattr(ds, "SeriesDescription", "") or "").strip(),
            }

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
            with self.lock:
                received = self._recv_counts.get(study_number, 0) + 1
                self._recv_counts[study_number] = received
            self.events.progress(study_number, "receive", received)

            # Accumulate the pre-anon extraction hints (first non-empty value
            # per field wins), and flush to disk once per series so a crash
            # mid-study doesn't lose them.
            self._merge_exam_hints_mem(study_uid, series_number, pre_anon_hints)

            # Update storage counters and persist sidecar for new study/series
            if is_new_series:
                base_path = self.base_path
                study_dir = base_path / f"{study_number:04d}"
                study_dir.mkdir(exist_ok=True)
                self._flush_exam_hints_sync(study_dir, self._exam_hints.get(study_uid))
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
            with self.lock:
                self._recv_counts.pop(study_number, None)

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
            sc = self.study_manager.get_sidecar_by_uid(study_uid)
            already_processed = sc is not None and sc.status in ("ready", "archived")
            if self.defacer.enabled and not already_processed:
                with self.lock:
                    self._active_defacing += 1
                try:
                    await asyncio.to_thread(self._deface_study, study_uid, study_number, study_dir)
                finally:
                    with self.lock:
                        self._active_defacing -= 1
            elif not already_processed:
                # Defacing is off, but still classify series so a head scan
                # is flagged (is_head=True, defaced=False) and the study is
                # marked defacing_disabled below instead of looking "ready".
                await asyncio.to_thread(
                    self._deface_study, study_uid, study_number, study_dir, classify_only=True
                )

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
            # A defacing pass that left a head-scan series undefaced must not
            # reach "ready" looking indistinguishable from one that fully
            # succeeded — manual_send_dicom/manual_upload_http both refuse to
            # export a study in that state.
            if self.study_manager.has_undefaced_head_series(study_uid):
                if self.defacer.enabled:
                    logger.warning(
                        "Study %04d has an undefaced head-scan series — marking "
                        "defacing_failed instead of ready; export is blocked until resolved",
                        study_number,
                    )
                    self.study_manager.mark_study_defacing_failed(study_uid)
                else:
                    logger.warning(
                        "Study %04d has a head-scan series but defacing is disabled — "
                        "marking defacing_disabled; export is blocked until defacing "
                        "is enabled and the study is reprocessed (restart)",
                        study_number,
                    )
                    self.study_manager.mark_study_defacing_disabled(study_uid)
            else:
                self.study_manager.mark_study_ready(study_uid)
                # If nothing is left to fill in (protocol picked, image
                # quality set, ...), the export ZIP + manifest are built now;
                # otherwise prepare_ready_exports() picks it up once the
                # operator clears the blockers on /studies.
                await asyncio.to_thread(
                    self._prepare_export_artifacts_sync, study_number
                )
        except DefacingCancelled:
            # Shutdown: the study keeps its "defacing" status, which is in the
            # restart re-queue set; series already defaced are skipped then.
            logger.info("Defacing of study %s interrupted by shutdown — it resumes on "
                        "restart", study_uid)
        except Exception:
            logger.exception("Unexpected error processing study %s", study_uid)
        finally:
            self._processing_studies.discard(study_uid)
            # The .exam_hints.json on disk is the record from here on.
            self._exam_hints.pop(study_uid, None)

    def _write_exam_sidecar(self, study_uid: str, study_number: int, study_dir: Path) -> None:
        """
        Blocking: derive RHYTHM exam-entry fields from study_dir's DICOM
        headers and write them to <study_number>_exam.json. Must run before
        export/retention can move or delete study_dir.
        """
        sc = self.study_manager.get_sidecar_by_uid(study_uid)
        anonymized_study_uid = sc.anonymized_study_uid if sc is not None else ""
        # In-memory hints are the most complete view; fall back to the
        # on-disk .exam_hints.json for a study re-queued after a restart.
        # Persist the winning view so a *later* restart + re-extract sees it
        # (the per-series flush during receive can lag by a series' images).
        hints = self._exam_hints.get(study_uid) or self._load_exam_hints_sync(study_dir)
        self._flush_exam_hints_sync(study_dir, hints)
        data = self.exam_extractor.extract(
            study_dir, study_number, anonymized_study_uid, hints=hints
        )
        ExamSidecar(study_number, data).save(self.base_path)
        self.events.publish("exam", n=study_number)

    # -- pre-anonymization extraction hints --------------------------------
    #
    # ExamExtractor runs on the completed, already-anonymized study. Under the
    # 'rhythm' profile that means Manufacturer / ManufacturerModelName /
    # ProtocolName / StudyDescription / SeriesDescription are gone by then, so
    # the protocol-catalog matches would all miss. These
    # helpers stash those five fields from each raw image before anonymize()
    # and hand them back to extract() as a fallback.

    _EXAM_HINT_STUDY_KEYS = ("manufacturer", "model", "protocol_name", "study_description")

    def _merge_exam_hints_mem(self, study_uid: str, series_number: int,
                              raw: Dict[str, str]) -> None:
        """First-non-empty merge of one image's *raw* hints into the in-memory
        per-study accumulator. *series_number* is the LOCAL folder number
        (arrival order), not the DICOM SeriesNumber — ExamExtractor looks
        series hints up by folder accordingly."""
        h = self._exam_hints.setdefault(study_uid, {"study": {}, "series": {}})
        for k in self._EXAM_HINT_STUDY_KEYS:
            if raw.get(k) and not h["study"].get(k):
                h["study"][k] = raw[k]
        sd = raw.get("series_description")
        skey = str(series_number)
        if sd and not h["series"].get(skey):
            h["series"][skey] = {"series_description": sd}

    @staticmethod
    def _flush_exam_hints_sync(study_dir: Path, hints: Optional[dict]) -> None:
        """Atomically write *hints* to <study_dir>/.exam_hints.json (no-op if
        empty). Excluded from every export ZIP by ZipManager."""
        if not hints:
            return
        dest = study_dir / ".exam_hints.json"
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            tmp.write_text(json.dumps(hints, indent=2))
            tmp.replace(dest)
        except OSError:
            logger.warning("Could not write %s", dest, exc_info=True)

    @staticmethod
    def _load_exam_hints_sync(study_dir: Path) -> Optional[dict]:
        path = study_dir / ".exam_hints.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("Unreadable %s — ignoring", path, exc_info=True)
            return None

    def _deface_study(self, study_uid: str, study_number: int, study_dir: Path,
                      classify_only: bool = False) -> None:
        """
        Blocking: deface all head-scan series in one study.
        Called via asyncio.to_thread — must not use await.

        With ``classify_only`` (defacing disabled) series are only classified
        as head/topogram/other in the sidecar; nothing is defaced or counted.
        """
        if not classify_only:
            self.study_manager.mark_study_defacing(study_uid)
            self.inc_counter('defacing', 'studies')
        series_dirs = [
            d for d in sorted(study_dir.iterdir())
            if d.is_dir() and not d.name.endswith("_pre_deface")
        ]
        for idx, series_dir in enumerate(series_dirs):
            if not classify_only:
                self.events.progress(study_number, "deface", idx, len(series_dirs),
                                     label=series_dir.name, force=True)

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
                if not classify_only:
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
                if classify_only:
                    logger.warning(
                        "Series %s in study %04d is a head scan but defacing is disabled",
                        series_dir.name, study_number,
                    )
                    continue
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
                if not classify_only:
                    self.inc_counter('defacing', 'series_skipped')
                if orig_series_uid:
                    self.study_manager.set_series_classification(
                        study_uid, orig_series_uid, is_head=False, is_topogram=False
                    )
        if not classify_only:
            self.events.progress(study_number, "deface", len(series_dirs), len(series_dirs))

    def _clear_deface_temp_sync(self, study_number: int) -> None:
        """Remove the persistent ``pixieveil_deface_<NNNN>_<SSSS>`` NIfTI
        scratch directories the Defacer keeps for manual inspection. Called
        once a study is exported (the scratch is intermediate working data
        with no audit value) or purged, so they don't accumulate."""
        for d in self.temp_path.glob(f"pixieveil_deface_{study_number:04d}_*"):
            shutil.rmtree(d, ignore_errors=True)

    def _sweep_orphan_deface_temp_sync(self) -> None:
        """Startup sweep: drop any ``pixieveil_deface_<NNNN>_*`` scratch dir
        whose study is already archived or no longer on disk. Newer code
        clears these at export time, but a study exported before this
        existed — or a crash between export and cleanup — leaves them behind.
        Scratch for a study still ``ready`` / ``defacing_failed`` /
        in-progress is kept for manual inspection."""
        if not self.temp_path.exists():
            return
        swept = 0
        for d in self.temp_path.glob("pixieveil_deface_????_*"):
            if not d.is_dir():
                continue
            name = d.name[len("pixieveil_deface_"):]
            num_str = name.split("_", 1)[0]
            if not num_str.isdigit():
                continue
            study_number = int(num_str)
            sc = self.study_manager.get_sidecar_by_number(study_number)
            study_dir = self.base_path / f"{study_number:04d}"
            if sc is None or sc.status == "archived" or not study_dir.exists():
                shutil.rmtree(d, ignore_errors=True)
                swept += 1
        if swept:
            logger.info("Swept %d orphaned deface scratch dir(s) from %s",
                        swept, self.temp_path)

    def _purge_study_artifacts_sync(self, study_number: int) -> None:
        """Delete everything bulky for a study but keep its records: the
        study directory, the ``export_path/<NNNN>.{zip,json}`` pair and the
        deface scratch dirs. The ``<NNNN>.state.json`` / ``<NNNN>.exam.json``
        sidecars are left in place as the permanent audit trail."""
        study_dir = self.base_path / f"{study_number:04d}"
        if study_dir.exists():
            shutil.rmtree(study_dir, ignore_errors=True)
        for suffix in (".zip", ".json"):
            (self.export_path / f"{study_number:04d}{suffix}").unlink(missing_ok=True)
        self._clear_deface_temp_sync(study_number)

    def _finish_export_sync(self, original_study_uid: str, study_number: int) -> None:
        """Bookkeeping after a study's first successful export by any method:
        stamp ``exported_at`` (the retention clock) and, when
        ``retain_after_export_days`` is not configured, purge the bulky
        artifacts immediately (prior always-delete behaviour). Otherwise the
        study directory + ``export_path`` pair sit until
        ``enforce_retention_purge`` ages them off. Mutations go through
        StudyManager so the in-memory sidecar cache stays consistent.

        The deface NIfTI scratch is intermediate working data with no audit
        value, so it is cleared now regardless of the retention window."""
        self.study_manager.mark_study_exported(original_study_uid, _now_iso())
        self._clear_deface_temp_sync(study_number)
        if not self.settings.storage.get("retain_after_export_days"):
            self._purge_study_artifacts_sync(study_number)
            self.study_manager.mark_study_purged(original_study_uid, _now_iso())

    def enforce_retention_purge_sync(self) -> None:
        """
        Purge exported studies whose retention window has elapsed: remove the
        study directory, the ``export_path/<NNNN>.{zip,json}`` pair and the
        deface scratch, keeping the ``.state.json`` / ``.exam.json`` records.
        No-op if ``storage.retain_after_export_days`` is not configured (the
        artifacts are then deleted at export time, not here).

        The retention clock is ``StudySidecar.exported_at`` (set on first
        successful export; for RHYTHM, when the ingest job reached COMPLETE).
        A study that was uploaded to RHYTHM but never reached COMPLETE has no
        ``exported_at`` — its ``export_path`` pair is the retry source and is
        kept indefinitely.
        """
        retain_days = self.settings.storage.get("retain_after_export_days")
        if not retain_days:
            return

        cutoff = datetime.now(timezone.utc).timestamp() - (retain_days * 86400)
        for sc in StudySidecar.load_all(self.base_path).values():
            # No exported_at → never purge: either a RHYTHM job that hasn't
            # confirmed COMPLETE (keep the retry source), or a study archived
            # before this field existed (its dir, if any, stays put).
            if sc.status != "archived" or sc.purged_at or not sc.exported_at:
                continue
            try:
                exported_ts = datetime.fromisoformat(sc.exported_at).timestamp()
            except ValueError:
                continue
            if exported_ts >= cutoff:
                continue
            self._purge_study_artifacts_sync(sc.study_number)
            self.study_manager.mark_study_purged(sc.original_study_uid, _now_iso())
            logger.info(
                "Retention: purged study %04d artifacts (exported %s, older "
                "than retain_after_export_days=%s) — records kept",
                sc.study_number, sc.exported_at, retain_days,
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
            # Also drop the export pair + deface scratch — same as a
            # retention purge, just triggered by disk pressure instead of age.
            self._purge_study_artifacts_sync(study_number)
            used_bytes -= dir_size
            self.study_manager.mark_study_purged(sc.original_study_uid, _now_iso())
            logger.info(
                f"Quota: removed study {study_dir.name} "
                f"({dir_size / (1024 ** 2):.1f} MB) — record kept"
            )

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
    # manual send/upload for a study marks it archived and stamps
    # exported_at (the retention clock); if retain_after_export_days is not
    # set, the study directory + export pair + deface scratch are removed
    # right then. A later manual re-send of an already-archived study just
    # re-sends — the "first export" bookkeeping is skipped.
    # -----------------------------------------------------------------

    def resolve_study_dir(self, study_number: int) -> tuple[Optional[Path], str]:
        """Locate a study's directory. Exported studies now stay in
        ``base_path`` until the retention window purges them, so there is
        only one place to look."""
        base_dir = self.base_path / f"{study_number:04d}"
        if base_dir.exists():
            return base_dir, "base"
        return None, "absent"

    def find_sidecar_by_number(self, study_number: int) -> Optional[StudySidecar]:
        return self.study_manager.get_sidecar_by_number(study_number)

    # Dashboard actions that, when they succeed, count as starting an export
    # (so their actor is stamped as the study's ``exported_by``).
    _EXPORT_ACTIONS = frozenset({"send_dicom", "upload_http", "upload_rhythm", "mark_exported"})

    def record_audit(self, study_number: int, user: Optional[str], action: str,
                     ok: bool, detail: Optional[str] = None) -> None:
        """Attribute a dashboard action to the user who ran it: log it and
        append it to the study sidecar's ``audit`` list (stamping
        ``exported_by`` on the first successful export action)."""
        actor = user or "unknown"
        logger.info("Audit: user=%s action=%s study=%04d ok=%s%s", actor, action,
                    study_number, ok, f" detail={detail}" if detail else "")
        sc = self.find_sidecar_by_number(study_number)
        if sc is None:
            return
        entry = {
            "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "user": actor, "action": action, "ok": bool(ok),
        }
        if detail:
            entry["detail"] = str(detail)[:500]
        exported_by = actor if ok and action in self._EXPORT_ACTIONS else None
        self.study_manager.append_audit(sc.original_study_uid, entry, exported_by=exported_by)

    # ------------------------------------------------------------------
    # Protocol catalog (config/protocols.yaml) — re-apply / inbox / usage

    def _catalog_inputs_sync(self, study_number: int, exam: dict) -> tuple:
        """(manufacturer, model, protocol_name, study_description) for matching.

        Prefers the pre-anonymization hints next to the study (the richest
        record), falling back to what the exam sidecar captured — which is
        all there is once a study's directory has been purged.
        """
        scanner = exam.get("scanner") or {}
        hint = exam.get("protocol_used_hint") or {}
        manufacturer, model = scanner.get("manufacturer"), scanner.get("model")
        protocol_name = hint.get("protocol_name")
        description = hint.get("study_description")
        hints = self._load_exam_hints_sync(self.base_path / f"{study_number:04d}") or {}
        study_hints = hints.get("study") or {}
        return (manufacturer or study_hints.get("manufacturer"),
                model or study_hints.get("model"),
                protocol_name or study_hints.get("protocol_name"),
                description or study_hints.get("study_description"))

    def _iter_exams_sync(self, *, include_archived: bool):
        """Yield (StudySidecar, ExamSidecar data) for studies with a readable exam."""
        for sc in sorted(StudySidecar.load_all(self.base_path).values(),
                         key=lambda s: s.study_number):
            if not include_archived and sc.status in ("archived", "receiving"):
                continue
            exam_path = ExamSidecar.path_for(self.base_path, sc.study_number)
            if not exam_path.exists():
                continue
            try:
                yield sc, ExamSidecar.load(exam_path).data
            except Exception:
                logger.warning("Corrupt exam sidecar %s — skipped", exam_path, exc_info=True)

    async def reapply_catalog_if_changed(self) -> list[int]:
        """Re-match studies when ``protocols.yaml`` changed since the last
        match — e.g. edited by hand — not only after a save on /protocols.
        Also once after start, since the file may have been edited while the
        server was down. A no-op for studies whose match didn't change."""
        version = await asyncio.to_thread(lambda: self.protocol_catalog.version)
        last = getattr(self, "_catalog_matched_version", None)
        if last == version:
            return []
        self._catalog_matched_version = version
        changed = await asyncio.to_thread(self.reapply_catalog_sync)
        logger.info("Protocol catalog changed on disk — re-matched %d study(ies)", len(changed))
        return changed

    def reapply_catalog_sync(self) -> list[int]:
        """Re-match every not-yet-exported study against the current catalog.

        Called after the catalog is edited. Values the operator set by hand
        (``manual.fields``) are left alone. Each changed exam is saved,
        announced on the event bus, and its export artifacts refreshed.
        Returns the study numbers that changed.
        """
        changed: list[int] = []
        for sc, exam in self._iter_exams_sync(include_archived=False):
            n = sc.study_number
            if n in self._manual_actions_in_progress:
                continue
            match = self.protocol_catalog.match(*self._catalog_inputs_sync(n, exam))
            if not exam_merge.apply_catalog_match(exam, match):
                continue
            ExamSidecar(n, exam).save(self.base_path)
            self.events.publish("exam", n=n)
            try:
                self._prepare_export_artifacts_sync(n)
            except Exception:
                logger.exception("Export artifact refresh failed for %04d", n)
            changed.append(n)
        if changed:
            logger.info("Protocol catalog re-applied to %d stud(y/ies): %s", len(changed),
                        ", ".join(f"{n:04d}" for n in changed))
        return changed

    def unmatched_protocols_sync(self) -> list[dict]:
        """Not-yet-exported studies the catalog doesn't match, grouped by
        (manufacturer, model, protocol name) — the /protocols page's inbox."""
        groups: dict[tuple, dict] = {}
        for sc, exam in self._iter_exams_sync(include_archived=False):
            manufacturer, model, name, description = self._catalog_inputs_sync(
                sc.study_number, exam)
            match = self.protocol_catalog.match(manufacturer, model, name, description)
            if match.matched:
                continue
            key = (manufacturer or "", model or "", match.protocol_name or "")
            g = groups.setdefault(key, {
                "manufacturer": manufacturer, "model": model,
                "protocol_name": match.protocol_name or None,
                "study_description": description,
                "scanner_id": match.scanner.id if match.scanner else None,
                "studies": [],
            })
            g["studies"].append(sc.study_number)
        return sorted(groups.values(), key=lambda g: (-len(g["studies"]),
                                                      g["manufacturer"] or "",
                                                      g["protocol_name"] or ""))

    def catalog_usage_sync(self) -> dict[str, int]:
        """protocol key → number of studies (any status) currently using it."""
        usage: dict[str, int] = {}
        for _sc, exam in self._iter_exams_sync(include_archived=True):
            key = (exam.get("rhythm") or {}).get("protocol_key")
            if key:
                usage[key] = usage.get(key, 0) + 1
        return usage

    # Fields exposed to callers (e.g. the dashboard) for each study in
    # list/detail responses. StudySidecar also carries original_study_uid /
    # original_patient_id (PHI) — never expose to_dict() directly, always go
    # through this whitelist.
    _STUDY_SIDECAR_FIELDS = (
        "study_number", "status", "archived_via",
        "received_at", "last_received_at", "anonymized_study_uid",
        "rhythm_batch", "rhythm_job_id", "rhythm_job_status_url", "rhythm_status",
        "exported_at", "exported_by",
    )

    @classmethod
    def _whitelist_sidecar(cls, sc: StudySidecar, location: str) -> dict:
        out = {field: getattr(sc, field) for field in cls._STUDY_SIDECAR_FIELDS}
        out["series_count"] = len(sc.series)
        out["location"] = location
        return out

    @staticmethod
    def _exam_summary(exam_data: Optional[dict]) -> Optional[dict]:
        if exam_data is None:
            return None
        scanner = exam_data.get("scanner") or {}
        rdsr = exam_data.get("rdsr") or {}
        manual = exam_data.get("manual") or {}
        patient = exam_data.get("patient") or {}
        missing = exam_merge.missing_required(exam_data)
        required = len(exam_merge.required_paths(exam_data))
        return {
            "protocol_type": exam_data.get("protocol_type"),
            "examination_group": exam_data.get("examination_group"),
            "indication": exam_data.get("indication"),
            "image_quality": exam_data.get("image_quality"),
            "scanner": {
                "manufacturer": scanner.get("manufacturer"),
                "model": scanner.get("model"),
            },
            "patient": {
                "age_years": patient.get("age_years"),
                "weight_kg": patient.get("weight_kg"),
            },
            "image_quality": exam_data.get("image_quality"),
            "total_dlp_mgy_cm": rdsr.get("total_dlp_mgy_cm"),
            "has_manual_edits": bool(manual.get("fields")),
            "missing_fields": missing,
            "completeness": {"filled": required - len(missing), "required": required},
        }

    # Every sidecar status a study can be in; get_status_counts_sync always
    # reports all of them (zero when absent) so callers need no defaults.
    _STUDY_STATUSES = (
        "receiving", "complete", "defacing", "defacing_failed", "defacing_disabled",
        "ready", "archived",
    )

    def get_status_counts_sync(self) -> dict:
        """Blocking: count studies per sidecar status (unknown statuses under 'other')."""
        counts = dict.fromkeys(self._STUDY_STATUSES, 0)
        for sc in StudySidecar.load_all(self.base_path).values():
            key = sc.status if sc.status in counts else "other"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def list_studies_summary_sync(self) -> List[dict]:
        """Blocking: enumerate every study sidecar plus its exam sidecar, if any."""
        sidecars = StudySidecar.load_all(self.base_path)
        results = []
        for sc in sorted(sidecars.values(), key=lambda s: s.study_number):
            _dir, location = self.resolve_study_dir(sc.study_number)
            entry = self._whitelist_sidecar(sc, location)

            exam_path = ExamSidecar.path_for(self.base_path, sc.study_number)
            exam_data = None
            exam_corrupt = False
            if exam_path.exists():
                try:
                    exam_data = ExamSidecar.load(exam_path).data
                except Exception:
                    logger.warning("Corrupt exam sidecar %s — skipping", exam_path, exc_info=True)
                    exam_corrupt = True
            # A corrupt sidecar must not render identically to "not yet
            # extracted" — a caller needs to know exam data exists but
            # couldn't be read, not just that the summary is empty.
            entry["exam_summary"] = self._exam_summary(exam_data)
            entry["exam_corrupt"] = exam_corrupt
            results.append(entry)
        return results

    def get_study_detail_sync(self, study_number: int) -> Optional[dict]:
        sc = self.find_sidecar_by_number(study_number)
        if sc is None:
            return None
        _dir, location = self.resolve_study_dir(study_number)
        study_info = self._whitelist_sidecar(sc, location)
        # Who did what (usernames only — no PHI); newest last.
        study_info["audit"] = list(sc.audit)

        exam_path = ExamSidecar.path_for(self.base_path, study_number)
        result: dict = {"study": study_info, "exam": None, "exam_corrupt": False,
                        "missing_fields": list(exam_merge.REQUIRED_PATHS),
                        "required_fields": list(exam_merge.REQUIRED_PATHS)}
        if not exam_path.exists():
            return result
        try:
            exam_data = ExamSidecar.load(exam_path).data
        except Exception:
            # Same policy as list_studies_summary_sync: report it, don't 500 —
            # the dashboard still needs the study so it can be re-extracted.
            logger.warning("Corrupt exam sidecar %s", exam_path, exc_info=True)
            result["exam_corrupt"] = True
            return result
        result["exam"] = exam_data
        result["missing_fields"] = exam_merge.missing_required(exam_data)
        result["required_fields"] = exam_merge.required_paths(exam_data)
        return result

    # Header tags the protocol draft can use (none of them identify a patient).
    _DRAFT_TAGS = ("KVP", "ExposureModulationType", "DataCollectionDiameter",
                   "ReconstructionDiameter", "ContrastBolusAgent", "ConvolutionKernel",
                   "SliceThickness", "SpiralPitchFactor", "XRayTubeCurrent", "SoftwareVersions")

    def protocol_draft_inputs_sync(self, study_number: int) -> Optional[tuple[dict, dict]]:
        """(effective exam, headers) for drafting a protocol from a study.

        *headers* holds :attr:`_DRAFT_TAGS` from the first non-localizer CT
        image (while the study is still on disk) plus the pre-anonymization
        scanner/protocol hints (Manufacturer, ManufacturerModelName,
        ProtocolName, StudyDescription). None if the study has no exam record.
        """
        exam_path = ExamSidecar.path_for(self.base_path, study_number)
        if not exam_path.exists():
            return None
        exam = ExamSidecar.load(exam_path).data
        headers: dict = {}
        study_dir, _loc = self.resolve_study_dir(study_number)
        if study_dir is not None:
            hints = (self._load_exam_hints_sync(study_dir) or {}).get("study") or {}
            for k in ("Manufacturer", "ManufacturerModelName", "ProtocolName",
                      "StudyDescription"):
                if hints.get(k):
                    headers[k] = hints[k]
            for series_dir in sorted(p for p in study_dir.iterdir()
                                     if p.is_dir() and not p.name.endswith("_pre_deface")):
                first = next(iter(sorted(series_dir.glob("*.dcm"))), None)
                if first is None:
                    continue
                try:
                    ds = pydicom.dcmread(first, stop_before_pixels=True)
                except Exception:
                    logger.debug("Draft: unreadable %s", first, exc_info=True)
                    continue
                if str(getattr(ds, "SOPClassUID", "")) not in self._RHYTHM_CT_SOP_CLASSES:
                    continue
                if "LOCALIZER" in [str(v).upper() for v in (ds.get("ImageType") or [])]:
                    continue
                for tag in self._DRAFT_TAGS:
                    value = ds.get(tag)
                    if value not in (None, ""):
                        headers[tag] = str(value)
                break
        return exam, headers

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
            blocked = self._deface_export_block(sc)
            if blocked:
                return {"ok": False, "message": blocked}
            first_export = sc is not None and sc.status != "archived"

            success = await self.dicom_storage.send_study(
                study_dir,
                progress=lambda done, total: self.events.progress(
                    study_number, "dicom", done, total),
            )
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
                    self.study_manager.mark_study_archived(
                        sc.original_study_uid, via="dicom"
                    )
                    await asyncio.to_thread(
                        self._finish_export_sync, sc.original_study_uid, study_number
                    )
                return {"ok": True, "message": f"sent to DICOM node ({location})"}

            with self.lock:
                self.inc_counter('export', 'errors')
                self.inc_counter('errors', 'total')
            return {"ok": False, "message": "DICOM send failed"}
        finally:
            self._manual_actions_in_progress.discard(study_number)
            with self.lock:
                self._active_exporting -= 1

    @staticmethod
    def _deface_export_block(sc) -> str | None:
        """Reason a study must not be exported for defacing reasons, or None."""
        if sc is None:
            return None
        if sc.status == "defacing_failed":
            return ("defacing did not complete for this study (a head-scan series "
                    "is still undefaced) — cannot export until that is resolved")
        if sc.status == "defacing_disabled":
            return ("this study contains a head-scan series but defacing is disabled — "
                    "enable defacing and restart to deface it before export")
        return None

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
            blocked = self._deface_export_block(sc)
            if blocked:
                return {"ok": False, "message": blocked}
            first_export = sc is not None and sc.status != "archived"

            # Build the ZIP into export_path (same shape RHYTHM uses). It
            # stays there: as the audit copy after a successful send, or a
            # record of the last attempt after a failed one (a retry
            # overwrites). Purged with the study directory once the retention
            # window elapses.
            await asyncio.to_thread(
                self.export_path.mkdir, parents=True, exist_ok=True
            )
            zip_path = await self.zip_manager.create_zip(
                f"{study_number:04d}", self.export_path, source_dir=study_dir
            )
            if not zip_path:
                with self.lock:
                    self.inc_counter('archive', 'errors')
                    self.inc_counter('errors', 'total')
                return {"ok": False, "message": "failed to create ZIP archive"}

            zip_size = await asyncio.to_thread(lambda: zip_path.stat().st_size)
            zip_sha = await asyncio.to_thread(sha256_file, zip_path)
            await asyncio.to_thread(
                self._write_http_export_descriptor_sync,
                study_number, zip_sha, zip_size, "attempted",
            )

            success = await self.remote_storage.upload_file(zip_path, zip_path.name)
            if success:
                await asyncio.to_thread(
                    self._write_http_export_descriptor_sync,
                    study_number, zip_sha, zip_size, "sent",
                )

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
                    self.study_manager.mark_study_archived(
                        sc.original_study_uid, via="http"
                    )
                    await asyncio.to_thread(
                        self._finish_export_sync, sc.original_study_uid, study_number
                    )
                return {"ok": True, "message": f"uploaded to HTTP storage ({location})"}

            with self.lock:
                self.inc_counter('export', 'errors')
                self.inc_counter('errors', 'total')
            return {"ok": False, "message": "HTTP upload failed"}
        finally:
            self._manual_actions_in_progress.discard(study_number)
            with self.lock:
                self._active_exporting -= 1

    # -- RHYTHM export (third path; see rhythm_storage / rhythm_manifest) --

    def _new_rhythm_batch_id(self) -> str:
        """One batch id per dashboard action. ``site_code`` + a UTC timestamp;
        no persisted sequence, since distinct actions are at least a second
        apart and RHYTHM only uses ``batch`` to group sibling items."""
        site = self.rhythm_storage.site_code or "SITE"
        return f"{site}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"

    # RHYTHM ("DICOM CT only, no reports or screenshots") — the only SOP
    # classes a series is allowed to carry into a RHYTHM upload. Anything
    # else (Secondary Capture dose-report screenshots, native RDSR, ...) is
    # dropped: an unrecognised class fails safe here rather than polluting
    # the repository, and the "no CT image series" guard then surfaces it.
    _RHYTHM_CT_SOP_CLASSES = frozenset({
        str(CTImageStorage),
        str(EnhancedCTImageStorage),
        str(LegacyConvertedEnhancedCTImageStorage),
    })

    def _rhythm_excluded_series_sync(self, study_dir: Path) -> set:
        """Series-folder names under *study_dir* to leave out of a RHYTHM
        upload: every series whose representative object is not a CT image
        (the rendered Dose Report Secondary Capture, a native RDSR, an
        unreadable file, ...).
        """
        excluded: set = set()
        for series_dir in study_dir.iterdir():
            if not series_dir.is_dir() or not series_dir.name.isdigit():
                continue
            first_dcm = next(series_dir.glob("*.dcm"), None)
            if first_dcm is None:
                continue
            try:
                ds = pydicom.dcmread(str(first_dcm), stop_before_pixels=True, force=True)
                sop_class = str(getattr(ds, "SOPClassUID", ""))
            except Exception:
                logger.warning(
                    "RHYTHM: could not classify series %s (%s) — excluding it",
                    series_dir.name, first_dcm, exc_info=True,
                )
                excluded.add(series_dir.name)
                continue
            if sop_class not in self._RHYTHM_CT_SOP_CLASSES:
                excluded.add(series_dir.name)
        return excluded

    # Tags the `rhythm` anonymization profile removes outright. If any still
    # carries a value on a CT slice, the study was anonymized under a
    # different profile — RHYTHM's GDPR-strict ingest validator would reject
    # every slice, so the study must be re-received under `rhythm` rather
    # than exported. Checked on one slice per series, cheap and decisive.
    _RHYTHM_STRIPPED_SENTINELS = (
        "DeviceSerialNumber", "StationName", "Manufacturer", "ProtocolName",
    )

    def _rhythm_anonymized_sync(self, study_dir: Path) -> bool:
        """True unless a CT slice still carries a tag the ``rhythm`` profile
        strips (i.e. the study was anonymized under another profile). A
        study with no readable CT slice returns True — the "no CT series"
        check handles that case."""
        if not study_dir.is_dir():
            return True
        for series_dir in sorted(study_dir.iterdir()):
            if not series_dir.is_dir() or not series_dir.name.isdigit():
                continue
            first_dcm = next(
                (f for f in sorted(series_dir.glob("*.dcm"))
                 if "_pre_deface" not in str(f)),
                None,
            )
            if first_dcm is None:
                continue
            try:
                ds = pydicom.dcmread(
                    str(first_dcm), stop_before_pixels=True, force=True
                )
            except Exception:
                continue
            if str(getattr(ds, "SOPClassUID", "")) not in self._RHYTHM_CT_SOP_CLASSES:
                continue
            return not any(
                str(getattr(ds, kw, "") or "").strip()
                for kw in self._RHYTHM_STRIPPED_SENTINELS
            )
        return True

    def _rhythm_manifest_item_sync(
        self, study_number: int
    ) -> Tuple[Optional[dict], Optional[str]]:
        """Build the RHYTHM v2 manifest item for one study from its effective
        exam sidecar. Returns ``(item, None)`` or ``(None, reason)``.

        ``sha256`` / ``size_bytes`` are not set here — they describe the
        export ZIP, which doesn't exist yet; ``manual_upload_rhythm`` fills
        them in after ``create_zip``. The on-disk exam sidecar's canonical
        values are already the effective ones (``exam_merge.apply_manual``
        writes manual edits into them at save time), so it feeds
        ``build_manifest_item`` directly.

        ``protocol_id`` is resolved from the protocol catalog
        using the local protocol key the operator picked for the study
        (``rhythm.protocol_key`` in the exam sidecar); an unset key, or one
        the map doesn't know, leaves ``protocol_id`` null — a blocker.
        """
        exam_path = ExamSidecar.path_for(self.base_path, study_number)
        if not exam_path.exists():
            return None, "exam data has not been extracted yet"
        try:
            exam_data = ExamSidecar.load(exam_path).data
        except Exception:
            logger.warning("Corrupt exam sidecar for %04d", study_number, exc_info=True)
            return None, "exam sidecar is unreadable"

        protocol_key = (exam_data.get("rhythm") or {}).get("protocol_key")
        pmap = self.rhythm_storage.protocol_map
        protocol_id = pmap.uuid_for(protocol_key)
        if protocol_key and protocol_id is None and pmap.entries:
            # Map is populated but this key doesn't resolve to a real UUID —
            # a misconfiguration, not just "protocol not picked yet".
            entry = pmap.get(protocol_key)
            if entry is not None and entry.uuid_pending:
                return None, (
                    f"protocol '{protocol_key}' has no RHYTHM UUID yet — paste the "
                    f"CTProtocol UUID from RHYTHM's Protocol Records page on the "
                    f"/protocols page"
                )
            return None, (
                f"protocol '{protocol_key}' is not in the protocol catalog — "
                f"add it on the /protocols page"
            )

        if not self._rhythm_anonymized_sync(self.base_path / f"{study_number:04d}"):
            return None, (
                "not anonymized under the 'rhythm' profile (device/date tags "
                "remain on CT slices) — RHYTHM's GDPR-strict validator would "
                "reject every slice; re-receive the source DICOM under 'rhythm'"
            )

        item = rhythm_manifest.build_manifest_item(
            exam_data,
            study_number=study_number,
            zip_filename=f"{study_number:04d}.zip",
            site_code=self.rhythm_storage.site_code,
            protocol_id=protocol_id,
        )
        return item, None

    def build_rhythm_manifest_preview_sync(self, study_numbers: List[int]) -> dict:
        """Build the RHYTHM v2 batch manifest for *study_numbers* without
        uploading anything — for inspecting or pasting into RHYTHM's own
        validate-manifest endpoint / json-validator page.

        Returns the manifest dict itself (top-level `type`/`items`/... so
        RHYTHM detects it as v2). ``sha256`` / ``size_bytes`` are omitted
        (computed from the export ZIP at upload time). Every item is still
        run through ``manifest_item_blockers``; anything that would stop a
        real upload is listed under an extra top-level ``blockers`` key
        (keyed by 4-digit study number) — RHYTHM's v2 schema allows extra
        properties, and the dashboard's download link strips it for a
        pristine file. ``site_code`` stays null when RHYTHM isn't configured
        and is reported as a blocker rather than back-filled with a
        placeholder.
        """
        site = self.rhythm_storage.site_code or "SITE"
        items: list[dict] = []
        blockers: dict[str, list[str]] = {}
        for n in study_numbers:
            item, err = self._rhythm_manifest_item_sync(n)
            if err:
                blockers[f"{n:04d}"] = [err]
                continue
            items.append(item)
            item_blockers = rhythm_manifest.manifest_item_blockers(item)
            if item_blockers:
                blockers[f"{n:04d}"] = item_blockers
        manifest = rhythm_manifest.build_batch_manifest(
            items,
            batch=self._new_rhythm_batch_id(),
            site_code=site,
            note="preview — sha256/size_bytes are added from the export ZIP at upload time",
        )
        if blockers:
            manifest["blockers"] = blockers
        return manifest

    # -- Proactive export artifacts (built when a study becomes export-ready,
    #    not when an upload button is clicked) -----------------------------

    def _export_data_blockers_sync(
        self, study_number: int
    ) -> Tuple[Optional[dict], list]:
        """``(item, blockers)`` for a study's RHYTHM manifest item, where
        *blockers* lists only the **study-data** problems that must be fixed
        before export artifacts can be built: study not ``ready``, missing
        ``protocol_id`` / ``image_quality``, unreadable exam sidecar, no CT
        series, or a study not anonymized under the ``rhythm`` profile.
        ``site_code`` is deliberately *not* counted — it is a
        destination-config value finalised at upload time, so a study can be
        packaged before RHYTHM/HTTP is configured."""
        sc = self.find_sidecar_by_number(study_number)
        if sc is None or sc.status != "ready":
            status = "unknown" if sc is None else sc.status
            return None, [f"study is not ready (status={status})"]

        item, err = self._rhythm_manifest_item_sync(study_number)
        if err:
            return None, [err]

        blockers = [
            b for b in rhythm_manifest.manifest_item_blockers(item)
            if not b.startswith("site_code")
        ]
        study_dir = self.base_path / f"{study_number:04d}"
        if study_dir.is_dir():
            excluded = self._rhythm_excluded_series_sync(study_dir)
            ct = {
                d.name for d in study_dir.iterdir()
                if d.is_dir() and d.name.isdigit()
            } - excluded
            if not ct:
                blockers.append("no CT image series")
        return item, blockers

    def _export_artifacts_current_sync(self, study_number: int) -> bool:
        """True when ``export_path/<NNNN>.{zip,json}`` both exist and are at
        least as new as every input (the study's ``.dcm`` files and its exam
        sidecar) — i.e. no rebuild is needed."""
        zip_path = self.export_path / f"{study_number:04d}.zip"
        json_path = self.export_path / f"{study_number:04d}.json"
        if not zip_path.exists() or not json_path.exists():
            return False
        try:
            artifact_mtime = min(
                zip_path.stat().st_mtime, json_path.stat().st_mtime
            )
        except OSError:
            return False
        # Self-heal manifests written by the pre-fix build (placeholder
        # "prepared" batch, no real site_code): force a rebuild so external
        # tooling never ships the sentinel.
        try:
            if json.loads(json_path.read_text()).get("batch") == "prepared":
                return False
        except (OSError, ValueError):
            return False
        study_dir = self.base_path / f"{study_number:04d}"
        inputs = list(study_dir.rglob("*.dcm")) if study_dir.is_dir() else []
        exam_path = ExamSidecar.path_for(self.base_path, study_number)
        if exam_path.exists():
            inputs.append(exam_path)
        try:
            newest_input = max((p.stat().st_mtime for p in inputs), default=0.0)
        except OSError:
            return False
        return artifact_mtime >= newest_input

    def _prepare_export_artifacts_sync(self, study_number: int) -> bool:
        """(Re)build ``export_path/<NNNN>.zip`` + ``<NNNN>.json`` for a study
        that is ``ready`` with every data blocker cleared. Rebuilds when the
        study directory or exam sidecar changed since the last build; removes
        the artifacts if the study no longer qualifies. Returns True when
        current artifacts are present afterwards."""
        zip_path = self.export_path / f"{study_number:04d}.zip"
        json_path = self.export_path / f"{study_number:04d}.json"

        item, blockers = self._export_data_blockers_sync(study_number)
        if blockers:
            removed = False
            for p in (zip_path, json_path):
                if p.exists():
                    p.unlink(missing_ok=True)
                    removed = True
            if removed:
                logger.info(
                    "Export: removed now-stale artifacts for %04d (%s)",
                    study_number, "; ".join(blockers),
                )
            return False

        if self._export_artifacts_current_sync(study_number):
            return True

        study_dir = self.base_path / f"{study_number:04d}"
        excluded = self._rhythm_excluded_series_sync(study_dir)
        self.export_path.mkdir(parents=True, exist_ok=True)
        built = self.zip_manager.create_zip_sync(
            f"{study_number:04d}", self.export_path, source_dir=study_dir,
            exclude=lambda p: p.parent.name in excluded,
        )
        if built is None:
            logger.warning("Export: failed to build ZIP for study %04d", study_number)
            return False

        assert item is not None  # implied by empty blockers
        item["sha256"] = sha256_file(built)
        item["size_bytes"] = built.stat().st_size
        # A real batch id + the configured site_code: this manifest is a
        # complete, shippable v2 document — external tooling that picks up
        # data/export/<NNNN>.json submits it as-is, and manual_upload_rhythm
        # rewrites it with its own batch when the operator sends from the
        # dashboard. site_code is left null (not a placeholder) when RHYTHM
        # is unconfigured, so a bad manifest fails loudly rather than
        # shipping "SITE".
        manifest = rhythm_manifest.build_batch_manifest(
            [item],
            batch=self._new_rhythm_batch_id(),
            site_code=self.rhythm_storage.site_code,
            note="prepared when the study became export-ready",
        )
        tmp = json_path.with_name(json_path.name + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.replace(json_path)
        logger.info("Export: prepared artifacts for study %04d", study_number)
        return True

    async def prepare_ready_exports(self) -> None:
        """Completion-loop tick: (re)build export artifacts for every
        ``ready`` study whose data blockers are cleared, and drop artifacts
        for any that no longer qualify. Self-healing — covers studies made
        ready before this existed and edits that changed the manifest."""
        def _run() -> None:
            for sc in StudySidecar.load_all(self.base_path).values():
                if (sc.status == "ready"
                        and sc.study_number not in self._manual_actions_in_progress):
                    try:
                        self._prepare_export_artifacts_sync(sc.study_number)
                    except Exception:
                        logger.warning(
                            "Export: prep failed for study %04d",
                            sc.study_number, exc_info=True,
                        )
        await asyncio.to_thread(_run)

    async def manual_upload_rhythm(self, study_number: int,
                                   *, batch: Optional[str] = None) -> dict:
        """Upload one ready study to the RHYTHM platform at the user's request.

        One study is one manifest item; *batch* groups sibling items from the
        same dashboard action and is generated here when not supplied.
        Refuses (does not raise) when RHYTHM export is unconfigured, the
        study is ``defacing_failed``, the anonymization profile is not
        ``gdpr``, the exam sidecar is missing/unreadable, a RHYTHM-required
        field is still unset (notably ``image_quality``), or the study holds
        no CT image series. The first successful export also does the
        retain-or-delete + ``mark_study_archived(via="rhythm")`` bookkeeping.
        """
        if study_number in self._manual_actions_in_progress:
            return {"ok": False, "message": "action already in progress for this study"}
        self._manual_actions_in_progress.add(study_number)
        with self.lock:
            self._active_exporting += 1
        try:
            study_dir, location = self.resolve_study_dir(study_number)
            if study_dir is None:
                return {"ok": False, "message": "study directory not found"}
            if not self.rhythm_storage.enabled:
                return {"ok": False, "message": "RHYTHM export is not configured"}

            profile = self.settings.anonymization.get("profile", "research")
            if profile != "rhythm":
                return {"ok": False, "message":
                        f"RHYTHM export requires the 'rhythm' anonymization profile "
                        f"(current: '{profile}') — other profiles leave tags "
                        f"(dates, device identity, descriptions, PatientID) that "
                        f"RHYTHM's GDPR-strict ingest validator rejects. This gate "
                        f"reads the current profile, not what each study was "
                        f"anonymized with; anonymization is one-shot at receive "
                        f"time, so re-send the source DICOM under 'rhythm' rather "
                        f"than re-exporting a study received under another profile"}

            sc = self.find_sidecar_by_number(study_number)
            if sc is not None and sc.status == "defacing_failed":
                return {"ok": False, "message":
                        "defacing did not complete for this study (a head-scan series "
                        "is still undefaced) — cannot export until that is resolved"}

            item, item_err = self._rhythm_manifest_item_sync(study_number)
            if item_err:
                return {"ok": False, "message": item_err}
            blockers = rhythm_manifest.manifest_item_blockers(item)
            if blockers:
                msg = "study is missing data RHYTHM requires: " + "; ".join(blockers)
                if any("protocol_id" in b for b in blockers):
                    msg += (" — map this study's protocol to a RHYTHM UUID on the "
                            "/protocols page, or pick one for the study")
                return {"ok": False, "message": msg}

            excluded = await asyncio.to_thread(
                self._rhythm_excluded_series_sync, study_dir
            )
            all_series = {
                d.name for d in study_dir.iterdir()
                if d.is_dir() and d.name.isdigit()
            }
            if not (all_series - excluded):
                return {"ok": False,
                        "message": "no CT image series to upload (only non-CT objects found)"}

            batch = batch or self._new_rhythm_batch_id()

            # Build the export ZIP + manifest into export_path BEFORE the
            # upload. They stay there as the audit copy / retry source: on a
            # failed or never-COMPLETE job they persist indefinitely; a
            # successful job's pair is aged off with the study directory once
            # the retention window elapses (a later attempt overwrites).
            await asyncio.to_thread(
                self.export_path.mkdir, parents=True, exist_ok=True
            )
            zip_path = await self.zip_manager.create_zip(
                f"{study_number:04d}", self.export_path, source_dir=study_dir,
                exclude=lambda p: p.parent.name in excluded,
            )
            if not zip_path:
                with self.lock:
                    self.inc_counter('archive', 'errors')
                    self.inc_counter('errors', 'total')
                return {"ok": False, "message": "failed to create ZIP archive"}

            # RHYTHM verifies sha256 before it extracts the archive, so the
            # manifest item's sha256/size_bytes are the ZIP's real values.
            zip_sha = await asyncio.to_thread(sha256_file, zip_path)
            item["sha256"] = zip_sha
            item["size_bytes"] = await asyncio.to_thread(
                lambda: zip_path.stat().st_size
            )
            await asyncio.to_thread(
                self._write_rhythm_export_manifest_sync, study_number, item, batch
            )

            result = await self.rhythm_storage.upload_study(
                zip_path, item, batch, file_hash=zip_sha,
                progress=lambda done, total: self.events.progress(
                    study_number, "rhythm", done, total),
            )

            # Persist whatever RHYTHM handed back — success or not — so the
            # dashboard can show job state / the failure reason.
            if sc is not None:
                self.study_manager.set_rhythm_job(
                    sc.original_study_uid,
                    batch=batch,
                    session_id=result.session_id,
                    job_id=result.job_id,
                    job_status_url=result.job_status_url,
                    status="uploaded" if result.ok else "failed",
                    zip_sha256=zip_sha,
                    zip_size=item["size_bytes"],
                )

            if result.ok:
                # upload_study returns once the chunks are assembled and an
                # ingest job is queued — NOT once ingest passed. The
                # retain-or-delete + mark_study_archived bookkeeping waits for
                # a terminal COMPLETE, which reconcile_rhythm_jobs() observes
                # from the completion loop. Study stays "ready" until then.
                return {"ok": True,
                        "message": f"queued for RHYTHM ingest (batch {batch}, "
                                   f"{location}) — processing",
                        "batch": batch, "job_id": result.job_id, "status": "uploaded"}

            with self.lock:
                self.inc_counter('export', 'errors')
                self.inc_counter('errors', 'total')
            return {"ok": False, "batch": batch,
                    "message": f"RHYTHM upload failed: {result.error}",
                    "error_report": result.error_report}
        finally:
            self._manual_actions_in_progress.discard(study_number)
            with self.lock:
                self._active_exporting -= 1

    async def manual_upload_rhythm_batch(self, study_numbers: List[int]) -> dict:
        """Upload several ready studies as one RHYTHM batch (one dashboard
        action). Each study is still validated and uploaded independently;
        one failing does not stop the rest."""
        batch = self._new_rhythm_batch_id()
        results: dict = {}
        for n in study_numbers:
            results[str(n)] = await self.manual_upload_rhythm(n, batch=batch)
        return {
            "ok": bool(results) and all(r.get("ok") for r in results.values()),
            "batch": batch,
            "results": results,
        }

    _MARK_EXPORTED_VIA = frozenset({"dicom", "http", "rhythm"})

    async def mark_exported(self, study_number: int,
                            *, via: str = "rhythm",
                            sha256: Optional[str] = None) -> dict:
        """Record that a ``ready`` study was exported out-of-band — e.g. by
        partner tooling that shipped ``export_path/<NNNN>.zip`` to RHYTHM
        itself. Runs the same bookkeeping a successful ``manual_*`` export
        would: ``mark_study_archived(via)`` + ``_finish_export_sync`` (stamp
        ``exported_at``, clear the deface scratch, and purge the study dir +
        export pair now if ``retain_after_export_days`` is unset).

        Refuses (does not raise) when the study is unknown, not ``ready``
        (``defacing_failed`` / still processing), or *via* is not one of
        ``dicom`` / ``http`` / ``rhythm``. An already-``archived`` study is a
        no-op success. When *sha256* is given it is checked against the
        prepared manifest and a mismatch is reported (but not fatal — the
        caller shipped it, we just record it)."""
        if via not in self._MARK_EXPORTED_VIA:
            return {"ok": False, "message": f"invalid via {via!r}"}
        if study_number in self._manual_actions_in_progress:
            return {"ok": False, "message": "action already in progress for this study"}
        self._manual_actions_in_progress.add(study_number)
        try:
            sc = self.find_sidecar_by_number(study_number)
            if sc is None:
                return {"ok": False, "message": "study not found"}
            if sc.status == "archived":
                return {"ok": True, "message": "already archived",
                        "status": "archived", "archived_via": sc.archived_via}
            if sc.status != "ready":
                return {"ok": False, "message":
                        f"study is '{sc.status}', not 'ready' — cannot mark exported"}

            warning = None
            if sha256 is not None:
                manifest = await asyncio.to_thread(
                    self.load_export_manifest_sync, study_number
                )
                on_disk = None
                if manifest and manifest.get("items"):
                    on_disk = manifest["items"][0].get("sha256")
                if on_disk and on_disk != sha256:
                    warning = (
                        f"sha256 mismatch: caller {sha256!r} vs prepared "
                        f"manifest {on_disk!r}"
                    )
                    logger.warning("mark_exported %04d: %s", study_number, warning)

            self.study_manager.mark_study_archived(sc.original_study_uid, via=via)
            await asyncio.to_thread(
                self._finish_export_sync, sc.original_study_uid, study_number
            )
            with self.lock:
                self.inc_counter('cleanup', 'studies')
            result = {"ok": True, "message": f"marked exported (via {via})",
                      "status": "archived", "archived_via": via}
            if warning:
                result["warning"] = warning
            return result
        finally:
            self._manual_actions_in_progress.discard(study_number)

    _RHYTHM_TERMINAL = frozenset({"COMPLETE", "PARTIAL", "FAILED"})

    async def reconcile_rhythm_jobs(self) -> None:
        """Poll RHYTHM once for every study whose ingest job is still in
        flight, and act on a terminal result.

        ``manual_upload_rhythm`` returns as soon as the chunks are assembled;
        RHYTHM then re-validates and pushes to Orthanc asynchronously. This
        runs from the completion loop: on ``COMPLETE`` it performs the
        retain-or-delete + ``mark_study_archived(via="rhythm")`` bookkeeping
        an inline export would have done; on ``PARTIAL`` / ``FAILED`` it just
        records the state (study stays ``ready`` so the operator can retry).
        """
        if not self.rhythm_storage.enabled:
            return
        for sc in StudySidecar.load_all(self.base_path).values():
            url = getattr(sc, "rhythm_job_status_url", None)
            if sc.rhythm_status == "COMPLETE" and not sc.rhythm_repository_study_id:
                # Backfill: COMPLETE before this existed, or the lookup
                # failed last time. Retried at most once per interval.
                await self._record_rhythm_study_ids(sc)
                continue
            if not url or sc.rhythm_status in self._RHYTHM_TERMINAL:
                continue
            if sc.study_number in self._manual_actions_in_progress:
                continue

            result = await self.rhythm_storage.check_job(url)
            if result.status not in self._RHYTHM_TERMINAL:
                continue  # still pending, or a transient poll failure
            live = self.study_manager.get_sidecar_by_number(sc.study_number)
            if (sc.study_number in self._manual_actions_in_progress
                    or live is None or live.rhythm_job_status_url != url):
                # A retry started while we were polling: this result belongs
                # to the superseded job — writing it would mark the new
                # attempt terminal and it would never be polled.
                logger.info("RHYTHM job %s for study %04d superseded by a retry; "
                            "ignoring its %s", url, sc.study_number, result.status)
                continue

            self.study_manager.set_rhythm_job(
                sc.original_study_uid, status=result.status
            )
            if result.status == "COMPLETE":
                await self._record_rhythm_study_ids(sc, force=True)
                if sc.status != "archived":
                    await self._finalize_rhythm_export(sc)
            else:
                logger.error(
                    "RHYTHM ingest for study %04d ended %s: %s",
                    sc.study_number, result.status, result.error_report,
                )

    RHYTHM_ID_RETRY_S = 3600

    async def _record_rhythm_study_ids(self, sc: StudySidecar, *, force: bool = False) -> None:
        """Fetch and store RHYTHM's Repository Study ID / Orthanc ID for a
        COMPLETE study (the patient -> RHYTHM correspondence an erasure
        request needs). Best-effort: a failure is logged and retried on a
        later tick, at most once per RHYTHM_ID_RETRY_S."""
        now = time.monotonic()
        last = self._rhythm_id_attempts.get(sc.study_number)
        if not force and last is not None and now - last < self.RHYTHM_ID_RETRY_S:
            return
        self._rhythm_id_attempts[sc.study_number] = now
        ids = await self.rhythm_storage.fetch_study_ids(sc.anonymized_study_uid)
        if not ids or not ids.get("repository_study_id"):
            logger.warning(
                "RHYTHM study %04d is COMPLETE but its Repository Study ID could not "
                "be read yet (study UID %s); will retry", sc.study_number,
                sc.anonymized_study_uid,
            )
            return
        self._rhythm_id_attempts.pop(sc.study_number, None)
        self.study_manager.set_rhythm_job(
            sc.original_study_uid,
            repository_study_id=ids["repository_study_id"],
            orthanc_study_id=ids.get("orthanc_study_id"),
        )
        logger.info("RHYTHM study %04d -> Repository Study ID %s",
                    sc.study_number, ids["repository_study_id"])

    async def _finalize_rhythm_export(self, sc: StudySidecar) -> None:
        """Archive a study whose RHYTHM job reached COMPLETE and start its
        retention clock. Mirrors the first-successful-export bookkeeping of
        the DICOM/HTTP paths. The ``export_path/<NNNN>.{zip,json}`` pair was
        written before the upload and is left in place (aged off with the
        study directory by ``enforce_retention_purge``)."""
        study_dir, _location = self.resolve_study_dir(sc.study_number)
        image_count = 0
        if study_dir is not None:
            excluded = await asyncio.to_thread(
                self._rhythm_excluded_series_sync, study_dir
            )
            image_count = await asyncio.to_thread(
                lambda: sum(
                    1 for f in study_dir.rglob("*.dcm")
                    if f.parent.name not in excluded
                    and not any(part.endswith("_pre_deface") for part in f.parts)
                )
            )
        with self.lock:
            self.inc_counter('export', 'rhythm_studies')
            self.inc_counter('export', 'rhythm_images', image_count)
            self.inc_counter('cleanup', 'studies')
            self.inc_counter('cleanup', 'images', image_count)

        self.study_manager.mark_study_archived(sc.original_study_uid, via="rhythm")
        await asyncio.to_thread(
            self._finish_export_sync, sc.original_study_uid, sc.study_number
        )
        logger.info("RHYTHM export of study %04d finalized (COMPLETE)", sc.study_number)

    def _write_rhythm_export_manifest_sync(
        self, study_number: int, item: dict, batch: str
    ) -> None:
        """Write ``export_path/<NNNN>.json`` — the RHYTHM v2 batch manifest
        for the ZIP being uploaded, with the ZIP's real ``sha256`` /
        ``size_bytes`` and its ``batch``. Written before the upload; a later
        attempt overwrites it."""
        self.export_path.mkdir(parents=True, exist_ok=True)
        manifest = rhythm_manifest.build_batch_manifest(
            [item],
            batch=batch,
            site_code=self.rhythm_storage.site_code or "SITE",
            note="export copy — the ZIP and manifest sent to RHYTHM for this study",
        )
        dest = self.export_path / f"{study_number:04d}.json"
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.replace(dest)
        logger.info("RHYTHM: wrote export manifest %s", dest)

    def _write_http_export_descriptor_sync(
        self, study_number: int, sha256: str, size_bytes: int, status: str,
    ) -> None:
        """Write ``export_path/<NNNN>.json`` — a short descriptor of the ZIP
        an HTTP export built, beside ``export_path/<NNNN>.zip``. Written with
        ``status="attempted"`` *before* the upload (so a failed send still
        leaves a record) and rewritten ``status="sent"`` on success — the
        file's presence alone does not imply the upload succeeded."""
        self.export_path.mkdir(parents=True, exist_ok=True)
        dest = self.export_path / f"{study_number:04d}.json"
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(json.dumps({
            "study_number": study_number,
            "exported_via": "http",
            "status": status,
            "written_at": _now_iso(),
            "zip": {
                "filename": f"{study_number:04d}.zip",
                "sha256": sha256,
                "size_bytes": size_bytes,
            },
        }, indent=2))
        tmp.replace(dest)

    def load_export_manifest_sync(self, study_number: int) -> Optional[dict]:
        """The manifest/descriptor written beside a study's export ZIP
        (``export_path/<NNNN>.json``), or ``None`` if there isn't one / it is
        unreadable. The dashboard serves this verbatim for an archived study
        instead of rebuilding a preview."""
        path = self.export_path / f"{study_number:04d}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("Unreadable export manifest: %s", path, exc_info=True)
            return None

    def _manual_reextract_sync(self, study_number: int, study_dir: Path) -> dict:
        exam_path = ExamSidecar.path_for(self.base_path, study_number)
        previous: dict = {}
        if exam_path.exists():
            # Deliberately not caught here: a previous sidecar that exists
            # but fails to load means whatever manual overlay it held (hand-
            # entered weight, an indication the lookup table couldn't
            # match, paper-note corrections) can't be recovered — silently
            # proceeding "without it" would discard that data while
            # manual_reextract() still reported ok:True. Let it propagate;
            # the caller's except turns it into a clear failure instead.
            previous = ExamSidecar.load(exam_path).data

        sc = self.find_sidecar_by_number(study_number)
        anonymized_study_uid = sc.anonymized_study_uid if sc is not None else ""

        # study is long past receive by now, so the hints only live on disk
        hints = self._load_exam_hints_sync(study_dir)
        fresh = self.exam_extractor.extract(
            study_dir, study_number, anonymized_study_uid, hints=hints
        )
        merged = exam_merge.merge_extracted(fresh, previous)
        ExamSidecar(study_number, merged).save(self.base_path)
        self.events.publish("exam", n=study_number)
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

    def _save_exam_sync(self, study_number: int, manual_fields: dict,
                        actor: Optional[str] = None) -> Optional[Tuple[dict, List[Path]]]:
        """Returns (exam_data, failed_dicom_paths) — the second element is
        non-empty when the sidecar itself saved fine but the weight/age
        DICOM write-back failed for one or more files, so save_exam() can
        report that instead of a bare "Saved." that isn't true for those
        files."""
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
        if actor:
            manual["edited_by"] = actor
        elif existing_manual.get("edited_by"):
            manual["edited_by"] = existing_manual["edited_by"]
        exam_merge.apply_manual(data, manual)
        exam_merge.recompute_buckets(data)
        ExamSidecar(study_number, data).save(self.base_path)
        self.events.publish("exam", n=study_number)

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
        failed_paths: List[Path] = []
        if weight_kg is not None or age_years is not None:
            sc = self.find_sidecar_by_number(study_number)
            if sc is not None and sc.status != "archived":
                study_dir, location = self.resolve_study_dir(study_number)
                if study_dir is not None and location == "base":
                    failed_paths = self._writeback_patient_fields_sync(
                        study_dir, weight_kg, age_years
                    )

        return data, failed_paths

    def _writeback_patient_fields_sync(self, study_dir: Path,
                                        weight_kg: Optional[float],
                                        age_years: Optional[float]) -> List[Path]:
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
            return []

        age_str = _format_dicom_age(age_years) if age_years is not None else None

        failed: List[Path] = []
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
                failed.append(dcm_path)
        return failed

    async def save_exam(self, study_number: int, manual_fields: dict,
                        actor: Optional[str] = None) -> dict:
        """Apply manually-entered fields to a study's exam sidecar and save it."""
        if study_number in self._manual_actions_in_progress:
            return {"ok": False, "message": "action already in progress for this study"}
        self._manual_actions_in_progress.add(study_number)
        try:
            result = await asyncio.to_thread(
                self._save_exam_sync, study_number, manual_fields, actor
            )
            if result is None:
                return {"ok": False, "message": "exam sidecar not found"}
            data, failed_paths = result
            # A manual edit may have cleared the last blocker (or changed a
            # value that's now baked into the DICOM) — refresh the export
            # artifacts to match.
            await asyncio.to_thread(
                self._prepare_export_artifacts_sync, study_number
            )
            if failed_paths:
                # The sidecar itself saved fine — data isn't lost — but the
                # correction didn't reach every DICOM file, so this must not
                # look like a plain "Saved." to the operator.
                return {
                    "ok": True,
                    "data": data,
                    "message": (
                        f"Saved, but failed to write weight/age into "
                        f"{len(failed_paths)} DICOM file(s) — see server logs"
                    ),
                }
            return {"ok": True, "data": data}
        finally:
            self._manual_actions_in_progress.discard(study_number)
