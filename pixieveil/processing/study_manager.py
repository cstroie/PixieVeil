"""
Study Manager Module

This module provides functionality for managing DICOM studies, including:
- Study state tracking and lifecycle management
- Study/series number assignment
- Image numbering within series
- Study completion monitoring
- Study timeout detection

Classes:
    StudyState: Tracks the state of a DICOM study
    StudyManager: Manages DICOM studies and their completion status
"""

import logging
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import pydicom

from pixieveil.config import Settings

logger = logging.getLogger(__name__)


class StudyState:
    """
    Tracks the state of a DICOM study.
    
    Attributes:
        last_received (float): Timestamp of the last received image for this study
        completed (bool): Flag indicating if the study has been completed and processed
    """
    
    def __init__(self):
        """Initialize a new StudyState instance."""
        self.last_received = time.time()
        self.completed = False


class StudyManager:
    """
    Manages DICOM studies and their complete lifecycle.
    
    This class provides comprehensive study management including:
    - Study state tracking (active, completed, etc.)
    - Numeric identifier assignment for studies and series
    - Image numbering within series
    - Study completion detection based on timeout
    - Thread-safe operations across multiple processing threads
    
    Attributes:
        settings (Settings): Application configuration settings
        study_states (Dict[str, StudyState]): Tracks state of each study by UID
        study_map (Dict[str, int]): Maps original StudyInstanceUID to numeric study number
        series_map (Dict[tuple, tuple]): Maps (StudyUID, SeriesUID) to (study_number, series_number)
        image_counters (Dict[tuple, int]): Tracks image numbers within each series
        study_counter (int): Counter for assigning numeric study IDs
        completion_timeout (int): Timeout in seconds for study completion
        lock (threading.Lock): Thread lock for thread-safe operations
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the StudyManager with application settings.
        
        Args:
            settings: Application configuration settings containing study timeout
                      and other configuration options
        """
        self.settings = settings
        self.completion_timeout = settings.study.get("completion_timeout", 120)
        
        # Study state tracking
        self.study_states: Dict[str, StudyState] = {}
        self.completed_count = 0
        
        # Study numbering
        self.study_map: Dict[str, int] = {}  # StudyInstanceUID -> study_number
        self.series_map: Dict[Tuple[str, str], Tuple[int, int]] = {}  # (StudyUID, SeriesUID) -> (study_num, series_num)
        self.image_counters: Dict[Tuple[int, int], int] = {}  # (study_num, series_num) -> image_counter
        self.study_counter = 0
        
        # Thread safety
        self.lock = threading.Lock()
        
        logger.debug("StudyManager initialized")
    
    def initialize_from_existing_studies(self, base_path: Path) -> None:
        """
        Initialize study counter from existing directories.
        
        This method scans the storage directory to find the highest existing
        study number and initializes the counter accordingly. Call this once
        at startup.
        
        Args:
            base_path (Path): Base directory containing organized studies
        """
        if not base_path.exists():
            logger.debug("Base path does not exist yet, starting from study counter 0")
            return
            
        existing_studies = [d.name for d in base_path.iterdir() if d.is_dir()]
        study_numbers = []
        
        for name in existing_studies:
            if len(name) == 4 and name.isdigit():
                study_numbers.append(int(name))
        
        with self.lock:
            self.study_counter = max(study_numbers) if study_numbers else 0
        
        logger.debug(f"Initialized study counter to {self.study_counter} from existing studies: {existing_studies}")
    
    def add_image_to_study(self, original_study_uid: str, original_series_uid: str) -> Tuple[int, int, int, bool]:
        """
        Add an image to a study and return assigned numeric IDs.

        This method:
        - Assigns numeric study number if new study
        - Assigns numeric series number if new series
        - Assigns numeric image number
        - Tracks study state and last received time

        Args:
            original_study_uid (str): Original StudyInstanceUID
            original_series_uid (str): Original SeriesInstanceUID

        Returns:
            Tuple[int, int, int, bool]: (study_number, series_number, image_number, is_new_series)
                - image_number: Atomically assigned image number for this image
                - is_new_series: True if this is the first image in a new series
        """
        with self.lock:
            # Handle study assignment
            if original_study_uid not in self.study_map:
                self.study_counter += 1
                self.study_map[original_study_uid] = self.study_counter
                logger.debug(f"Assigned new study number {self.study_counter} to study {original_study_uid}")

            study_number = self.study_map[original_study_uid]

            # Handle series assignment
            series_key = (original_study_uid, original_series_uid)
            is_new_series = series_key not in self.series_map

            if is_new_series:
                # Find highest existing series number for this study
                study_series = [series_num for (sid, suid), (sn, series_num) in self.series_map.items()
                               if sn == study_number]
                series_number = max(study_series) + 1 if study_series else 1
                self.series_map[series_key] = (study_number, series_number)
                logger.debug(f"Assigned new series number {series_number} to series {original_series_uid} in study {study_number}")
            else:
                study_number, series_number = self.series_map[series_key]

            # Handle image numbering — done atomically inside the lock to prevent
            # two concurrent threads from receiving the same image number
            image_key = (study_number, series_number)
            if image_key not in self.image_counters:
                self.image_counters[image_key] = 0
            self.image_counters[image_key] += 1
            image_number = self.image_counters[image_key]

            # Update study state
            if original_study_uid not in self.study_states:
                self.study_states[original_study_uid] = StudyState()
                logger.debug(f"Created new StudyState for study {original_study_uid}")
            else:
                self.study_states[original_study_uid].last_received = time.time()

            return study_number, series_number, image_number, is_new_series
    
    def get_next_image_number(self, study_number: int, series_number: int) -> int:
        """
        Get the next image number for a series.
        
        Args:
            study_number (int): Numeric study number
            series_number (int): Numeric series number
            
        Returns:
            int: The next available image number for this series
        """
        with self.lock:
            image_key = (study_number, series_number)
            return self.image_counters.get(image_key, 0)
    
    def check_study_completions(self) -> list[str]:
        """
        Check for completed studies based on timeout.
        
        Returns a list of StudyInstanceUIDs that have timed out and should
        be processed (archived, zipped, uploaded).
        
        Returns:
            list[str]: List of original StudyInstanceUIDs that are completed
        """
        now = time.time()
        completed_studies = []
        
        with self.lock:
            for study_uid, state in list(self.study_states.items()):
                if not state.completed and (now - state.last_received) > self.completion_timeout:
                    logger.info(f"Study {study_uid} timed out after {now - state.last_received:.1f}s")
                    completed_studies.append(study_uid)
                    state.completed = True
                    self.completed_count += 1
        
        return completed_studies
    
    def mark_study_archived(self, original_study_uid: str) -> None:
        """
        Mark a study as archived and clean up.
        
        Args:
            original_study_uid (str): The original StudyInstanceUID
        """
        with self.lock:
            if original_study_uid in self.study_states:
                logger.debug(f"Marking study {original_study_uid} as archived and removing from tracking")
                del self.study_states[original_study_uid]
    
    def get_study_number(self, original_study_uid: str) -> Optional[int]:
        """
        Get the numeric study number for an original StudyInstanceUID.
        
        Args:
            original_study_uid (str): The original StudyInstanceUID
            
        Returns:
            Optional[int]: The numeric study number or None if not found
        """
        with self.lock:
            return self.study_map.get(original_study_uid)
    
    def get_active_study_numbers(self) -> set:
        """Get the set of study numbers for studies still active (not yet archived)."""
        with self.lock:
            return {num for uid, num in self.study_map.items()
                    if uid in self.study_states and not self.study_states[uid].completed}

    def get_active_study_count(self) -> int:
        """Get count of active (not completed) studies."""
        with self.lock:
            return sum(1 for state in self.study_states.values() if not state.completed)
    
    def get_completed_study_count(self) -> int:
        """Get count of completed studies."""
        with self.lock:
            return self.completed_count

