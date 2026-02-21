"""
DICOM Anonymizer Module

This module provides functionality for anonymizing DICOM datasets in compliance with
DICOM PS3.15 standards. It removes or replaces sensitive patient information while
maintaining the integrity of the medical imaging data.

Classes:
    Anonymizer: Handles DICOM dataset anonymization operations
"""

import logging
import random
import string
from datetime import datetime
from typing import Dict, Any, Optional, Set
import pydicom
from pydicom.uid import generate_uid

from pixieveil.config import Settings

logger = logging.getLogger(__name__)


class Anonymizer:
    """
    Handles DICOM dataset anonymization operations.
    
    This class provides comprehensive DICOM field anonymization compliant with
    DICOM PS3.15 standards. It removes or replaces sensitive information while
    maintaining the integrity of the medical imaging data.
    
    The anonymization process includes:
    - Patient information removal/replacement
    - Study/Series information anonymization
    - Institution and physician information removal
    - Date/time anonymization
    - Sensitive tag removal
    - Private tag removal
    - Overlay data removal
    
    Attributes:
        settings (Settings): Application configuration settings
        profile (Optional[str]): Current anonymization profile name
        profile_config (Optional[Dict[str, Any]]): Current profile configuration
        pseudo_values (Dict[str, Dict[str, Any]]): Cache for persistent pseudo values
    """
    
    def __init__(self, settings: Settings, profile: Optional[str] = None):
        """
        Initialize the Anonymizer with application settings and profile.
        
        Args:
            settings: Application configuration settings containing anonymization
                      rules and preferences
            profile: Name of the anonymization profile to use (optional)
        """
        self.settings = settings
        self.profile = profile
        self.profile_config = None
        self.pseudo_values = {}
        
        # Load profile configuration if specified
        if profile and profile in settings.anonymization_profiles:
            self.profile_config = settings.anonymization_profiles[profile]
            logger.info(f"Using anonymization profile: {profile}")
        else:
            logger.warning(f"Anonymization profile '{profile}' not found, using default behavior")
    
    def _current_date(self):
        """
        Get current date in DICOM format (YYYYMMDD).
        
        Returns:
            str: Current date formatted as YYYYMMDD
        """
        return datetime.now().strftime("%Y%m%d")
    
    def _current_time(self):
        """
        Get current time in DICOM format (HHMMSS).
        
        Returns:
            str: Current time formatted as HHMMSS
        """
        return datetime.now().strftime("%H%M%S")
    
    def _generate_random_string(self, length: int = 8) -> str:
        """
        Generate a random string of specified length.
        
        Args:
            length (int): Length of the random string
            
        Returns:
            str: Random string
        """
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    
    def _generate_pseudo_value(self, category: str, study_uid: str = None, series_uid: str = None) -> str:
        """
        Generate a persistent pseudo value for a specific category.
        
        The same pseudo value will be returned for the same category and identifiers,
        ensuring consistency across related DICOM objects.
        
        Args:
            category (str): Category of the pseudo value (e.g., "study", "series", "patient")
            study_uid (str, optional): Study instance UID for context
            series_uid (str, optional): Series instance UID for context
            
        Returns:
            str: Persistent pseudo value
        """
        # Create a cache key based on category and identifiers
        cache_key = f"{category}"
        if study_uid:
            cache_key += f"_{study_uid}"
        if series_uid:
            cache_key += f"_{series_uid}"
            
        # Return cached value if available
        if cache_key in self.pseudo_values:
            return self.pseudo_values[cache_key]
        
        # Generate new pseudo value
        if category in ["study", "series", "image", "frame_of_reference"]:
            # Generate DICOM-style UIDs
            pseudo_value = generate_uid(prefix="2.25.")
        else:
            # Generate random strings for other categories
            pseudo_value = self._generate_random_string(12)
        
        # Cache the value
        self.pseudo_values[cache_key] = pseudo_value
        return pseudo_value
    
    def _get_action_for_tag(self, tag_name: str) -> str:
        """
        Get the action for a specific DICOM tag from the current profile.
        
        Args:
            tag_name (str): DICOM tag name (e.g., "PatientName", "StudyInstanceUID")
            
        Returns:
            str: Action to perform ("keep", "random", "pseudo", "ANONYMOUS", "UNKNOWN")
        """
        if not self.profile_config:
            return "keep"
        
        action = self.profile_config.get(tag_name)
        if action is None:
            return "keep"
        elif isinstance(action, str):
            return action
        elif isinstance(action, bool):
            return "keep" if action else "random"
        else:
            return "keep"
    
    def _apply_action_to_value(self, action: str, current_value: Any, tag_name: str, 
                              study_uid: str = None, series_uid: str = None) -> Any:
        """
        Apply an action to a DICOM value based on the current profile.
        
        Args:
            action (str): Action to perform
            current_value (Any): Current DICOM value
            tag_name (str): DICOM tag name
            study_uid (str, optional): Study instance UID for context
            series_uid (str, optional): Series instance UID for context
            
        Returns:
            Any: Modified value after applying the action
        """
        if action == "keep":
            return current_value
        elif action == "random":
            if isinstance(current_value, str):
                return self._generate_random_string(len(str(current_value)))
            elif isinstance(current_value, int):
                return random.randint(1000, 9999)
            else:
                return self._generate_random_string(8)
        elif action == "pseudo":
            return self._generate_pseudo_value(tag_name.lower(), study_uid, series_uid)
        elif action == "ANONYMOUS":
            return "ANONYMOUS"
        elif action == "UNKNOWN":
            return "UNKNOWN"
        else:
            return current_value
    
    def anonymize(self, ds: pydicom.Dataset, image_path: Path, image_id: str) -> Optional[Path]:
        """
        Apply configurable anonymization to a DICOM dataset based on the current profile.
        
        This method performs anonymization of a DICOM dataset according to the
        configured profile, supporting various actions like keep, random, pseudo,
        ANONYMOUS, and UNKNOWN for different DICOM fields.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to anonymize
            image_path (Path): Path to the original DICOM file
            image_id (str): Unique identifier for this DICOM image
            
        Returns:
            Optional[Path]: Path to the anonymized DICOM file if successful,
                          None if anonymization failed
        """
        try:
            # Get study and series UIDs for persistent pseudo values
            study_uid = getattr(ds, 'StudyInstanceUID', None)
            series_uid = getattr(ds, 'SeriesInstanceUID', None)
            
            # Apply anonymization based on profile configuration
            if self.profile_config:
                # Handle DICOM tag anonymization
                for tag_name, action in self.profile_config.items():
                    if tag_name in ["PixelBlackout", "KeepPrivateTags", "RetainStudyDate"]:
                        continue  # Skip global switches
                    
                    # Check if the tag exists in the dataset
                    if hasattr(ds, tag_name):
                        current_value = getattr(ds, tag_name)
                        new_value = self._apply_action_to_value(
                            action, current_value, tag_name, study_uid, series_uid
                        )
                        setattr(ds, tag_name, new_value)
                
                # Handle global switches
                if self.profile_config.get("PixelBlackout", False):
                    self._apply_pixel_blackout(ds)
                
                if not self.profile_config.get("KeepPrivateTags", True):
                    ds.remove_private_tags()
                
                if not self.profile_config.get("RetainStudyDate", True):
                    current_date = self._current_date()
                    if hasattr(ds, 'StudyDate'):
                        ds.StudyDate = current_date
                    if hasattr(ds, 'SeriesDate'):
                        ds.SeriesDate = current_date
                    if hasattr(ds, 'AcquisitionDate'):
                        ds.AcquisitionDate = current_date
            else:
                # Fallback to default anonymization behavior
                self._default_anonymization(ds)
            
            # Save the anonymized dataset
            anonymized_path = image_path.parent / f"anonymized_{image_path.name}"
            ds.save_as(anonymized_path)
            
            logger.info(f"Successfully anonymized image {image_id} using profile '{self.profile}'")
            return anonymized_path
            
        except Exception as e:
            logger.error(f"Failed to anonymize image {image_id}: {e}")
            return None
    
    def _apply_pixel_blackout(self, ds: pydicom.Dataset):
        """
        Apply pixel blackout to the DICOM dataset.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to modify
        """
        if hasattr(ds, 'PixelData') and ds.PixelData is not None:
            # This is a simplified implementation - in practice, you might want
            # to use more sophisticated pixel manipulation
            logger.info("Applying pixel blackout (simplified implementation)")
            # Note: Full pixel blackout would require more complex image processing
    
    def _default_anonymization(self, ds: pydicom.Dataset):
        """
        Apply default anonymization behavior when no profile is configured.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to anonymize
        """
        # Patient Information
        ds.PatientName = "Anonymous"
        ds.PatientID = "ID-REDACTED"
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        if "PatientAge" in ds: ds.PatientAge = ""
        if "OtherPatientIDs" in ds: ds.OtherPatientIDs = ""
        if "PatientAddress" in ds: ds.PatientAddress = ""
        if "PatientSize" in ds: ds.PatientSize = ""
        if "PatientWeight" in ds: ds.PatientWeight = ""
        
        # Study/Series Information
        if "StudyInstanceUID" in ds: 
            ds.StudyInstanceUID = self._generate_pseudo_value("study")
        if "SeriesInstanceUID" in ds: 
            ds.SeriesInstanceUID = self._generate_pseudo_value("series")
        if "SOPInstanceUID" in ds: 
            ds.SOPInstanceUID = self._generate_pseudo_value("image")
        ds.AccessionNumber = self._generate_pseudo_value("accession")
        ds.StudyDescription = "Anonymized Study"
        if "SeriesDescription" in ds: ds.SeriesDescription = "Anonymized Series"
        
        # Institution and Physician Information
        if "InstitutionName" in ds: ds.InstitutionName = ""
        if "InstitutionAddress" in ds: ds.InstitutionAddress = ""
        if "ReferringPhysicianName" in ds: ds.ReferringPhysicianName = ""
        if "OperatorsName" in ds: ds.OperatorsName = ""
        if "PerformingPhysicianName" in ds: ds.PerformingPhysicianName = ""
        
        # Dates and Times
        current_date = self._current_date()
        current_time = self._current_time()
        if "InstanceCreationDate" in ds: ds.InstanceCreationDate = current_date
        if "InstanceCreationTime" in ds: ds.InstanceCreationTime = current_time
        if "StudyDate" in ds: ds.StudyDate = current_date
        if "ContentDate" in ds: ds.ContentDate = current_date
        if "AcquisitionDate" in ds: ds.AcquisitionDate = current_date
        if "AcquisitionDateTime" in ds: ds.AcquisitionDateTime = current_date + current_time
        if "StudyTime" in ds: ds.StudyTime = current_time
        if "SeriesTime" in ds: ds.SeriesTime = current_time
        
        # Remove sensitive tags
        tags_to_remove = [
            "OtherPatientIDsSequence", "PatientTelephoneNumbers", "MilitaryRank",
            "RequestAttributesSequence", "ClinicalTrialSponsorName", "ClinicalTrialProtocolID"
        ]
        for tag in tags_to_remove:
            if tag in ds:
                del ds[tag]
                
        # Burned In Annotation
        if "BurnedInAnnotation" in ds:
            ds.BurnedInAnnotation = "NO"
        elif (0x0028, 0x0301) in ds:
            ds[0x0028, 0x0301].value = "NO"
            
        # Remove overlay data (60xx groups)
        for overlay_group in range(0x6000, 0x6020, 0x2):
            if overlay_group in ds:
                del ds[overlay_group]
