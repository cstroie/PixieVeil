"""
DICOM Anonymizer Module

This module provides functionality for anonymizing DICOM datasets in compliance with
DICOM PS3.15 standards. It removes or replaces sensitive patient information while
maintaining the integrity of the medical imaging data.

Classes:
    Anonymizer: Handles DICOM dataset anonymization operations
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any
import hashlib
import pydicom
from pydicom.uid import generate_uid

from pixieveil.config import Settings, AnonymizationProfile

logger = logging.getLogger(__name__)


class Anonymizer:
    """
    Handles DICOM dataset anonymization operations using profile-based configuration.
    
    This class provides comprehensive DICOM field anonymization compliant with
    DICOM PS3.15 standards. It removes or replaces sensitive patient information
    while maintaining the integrity of the medical imaging data.

    Anonymization behavior is driven by profiles defined in the configuration.
    Each profile specifies how individual DICOM fields should be handled.
    
    The anonymization process includes:
    - Patient information replacement/removal per profile
    - Study/Series information anonymization per profile
    - Institution and physician information handling per profile
    - Date/time anonymization (optionally preserved per profile)
    - Sensitive tag removal
    - Private tag removal (optional per profile)
    - Overlay data removal
    - Pixel data blackout (optional per profile)
    
    Attributes:
        settings (Settings): Application configuration settings
        profile (AnonymizationProfile): The active anonymization profile
    """
    
    def __init__(self, settings: Settings, profile_name: Optional[str] = None):
        """
        Initialize the Anonymizer with application settings and profile.
        
        Args:
            settings: Application configuration settings
            profile_name (str, optional): Name of the profile to use. If None,
                                        uses the default profile from settings.
                                        
        Raises:
            ValueError: If the specified profile does not exist
        """
        self.settings = settings
        self.profile = settings.get_anonymization_profile(profile_name)
        
        # Mappings to ensure consistency across studies and series
        self._pseudonym_map = {}  # Maps original value -> pseudonym for "PSEUDO" strategy
        self._study_uid_map = {}  # Maps original StudyInstanceUID -> anonymized UID
        self._series_uid_map = {}  # Maps original SeriesInstanceUID -> anonymized UID
        
        logger.info(f"Anonymizer initialized with profile: {profile_name or settings.anonymization.get('profile', 'research')}")
        
    def current_date(self) -> str:
        """
        Get current date in DICOM format (YYYYMMDD).
        
        Returns:
            str: Current date formatted as YYYYMMDD
        """
        return datetime.now().strftime("%Y%m%d")
    
    def current_time(self) -> str:
        """
        Get current time in DICOM format (HHMMSS).
        
        Returns:
            str: Current time formatted as HHMMSS
        """
        return datetime.now().strftime("%H%M%S")
    
    def generate_new_uid(self, prefix: str = "2.25.") -> str:
        """
        Generate a new DICOM UID with the specified prefix.
        
        Args:
            prefix (str): Prefix for the generated UID (default: "2.25.")
            
        Returns:
            str: Newly generated DICOM UID
        """
        return generate_uid(prefix=prefix)
    
    def generate_pseudonym(self, original_value: str) -> str:
        """
        Generate a consistent pseudonym from an original value using deterministic hashing.
        
        This ensures that the same original value always maps to the same pseudonym,
        which is critical for maintaining consistency across multiple files.
        
        Args:
            original_value (str): The original value to pseudonymize
            
        Returns:
            str: A deterministic pseudonym based on the original value
        """
        original_str = str(original_value)
        if original_str not in self._pseudonym_map:
            # Generate a hash-based pseudonym
            hash_obj = hashlib.sha256(original_str.encode())
            hash_hex = hash_obj.hexdigest()[:8].upper()
            self._pseudonym_map[original_str] = hash_hex
        return self._pseudonym_map[original_str]
    
    def generate_pseudonym_uid(self, original_uid: str) -> str:
        """
        Generate a consistent pseudonym UID from an original UID.
        
        Args:
            original_uid (str): The original UID to pseudonymize
            
        Returns:
            str: A deterministic pseudonym UID
        """
        original_str = str(original_uid)
        if original_str not in self._pseudonym_map:
            # Generate a hash-based pseudonym that looks like a UID
            hash_obj = hashlib.sha256(original_str.encode())
            hash_int = int(hash_obj.hexdigest()[:15], 16)
            self._pseudonym_map[original_str] = f"2.25.{hash_int}"
        return self._pseudonym_map[original_str]
    
    def apply_field_value_strategy(self, original_value: Any, strategy: Optional[str], 
                                   field_name: str = "") -> Optional[str]:
        """
        Apply a field value strategy to transform an original value.
        
        Strategies:
        - None: return None (field should be cleared)
        - "PSEUDO": return deterministic pseudonym
        - "NEWUID": return new generated UID
        - "KEEP": return original value unchanged
        - "CLEAR": return empty string
        - string literal: return the strategy string as-is
        
        Args:
            original_value: The original DICOM field value
            strategy: The strategy to apply (see above)
            field_name: Name of field for logging (optional)
            
        Returns:
            Optional[str]: The transformed value, or None to clear the field
        """
        if strategy is None:
            return None
        elif strategy.upper() == "PSEUDO":
            return self.generate_pseudonym(original_value)
        elif strategy.upper() == "PSEUDOUID":
            return self.generate_pseudonym_uid(original_value)
        elif strategy.upper() == "NEWUID":
            return self.generate_new_uid()
        elif strategy.upper() == "KEEP":
            return original_value
        elif strategy.upper() == "CLEAR":
            return ""
        else:
            # Literal string value
            return str(strategy)
    
    def set_field(self, ds: pydicom.Dataset, field_name: str, value: Optional[str]) -> None:
        """
        Set a DICOM field to a value, or clear it if value is None.
        
        Args:
            ds: The DICOM dataset
            field_name: Name of the field to set
            value: The value to set, or None to clear
        """
        if field_name not in ds:
            return
        
        if value is None:
            setattr(ds, field_name, "")
        else:
            setattr(ds, field_name, value)
    
    def apply_uid_mapping(self, original_uid: str, mapping_dict: Dict[str, str], 
                          strategy: Optional[str]) -> str:
        """
        Apply UID mapping strategy with proper consistency handling.
        
        Args:
            original_uid: The original UID
            mapping_dict: Dictionary to store mappings for consistency
            strategy: Strategy to apply
            
        Returns:
            str: The mapped/generated UID
        """
        if original_uid not in mapping_dict:
            new_value = self.apply_field_value_strategy(original_uid, strategy, field_name="UID")
            mapping_dict[original_uid] = new_value if new_value is not None else ""
        return mapping_dict[original_uid]

    def anonymize(self, ds: pydicom.Dataset) -> pydicom.Dataset:
        """
        Comprehensive DICOM field anonymization using the active profile.
        
        This method performs anonymization of a DICOM dataset according to the
        active profile configuration, removing or replacing sensitive information
        while maintaining data integrity for medical imaging purposes.

        The anonymization process includes:
        - Patient information anonymization (name, ID, demographics)
        - Study/Series information anonymization (UIDs, descriptions)
        - Institution and physician information removal/replacement
        - Date/time fields anonymization (optionally preserved per profile)
        - Sensitive tag removal
        - Private tag removal (if configured)
        - Overlay data removal
        - Pixel data blackout (if configured)
        - Burned-in annotation handling
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to anonymize
            
        Returns:
            pydicom.Dataset: The anonymized DICOM dataset
            
        Note:
            This method modifies the dataset in-place and also returns it
            for method chaining convenience.
        """
        # Apply profile-based field transformations
        self.anonymize_patient_fields(ds)
        self.anonymize_study_series_fields(ds)
        self.anonymize_institution_physician_fields(ds)
        self.anonymize_dates(ds)
        self.remove_sensitive_tags(ds)
        self.handle_private_tags(ds)
        self.remove_overlays(ds)
        self.handle_pixel_blackout(ds)
        
        # Burned In Annotation
        if "BurnedInAnnotation" in ds:
            ds.BurnedInAnnotation = "NO"
        elif (0x0028, 0x0301) in ds:
            ds[0x0028, 0x0301].value = "NO"
        
        logger.debug(f"Successfully anonymized image using profile with PixelBlackout={self.profile.PixelBlackout}")
        return ds
    
    def anonymize_patient_fields(self, ds: pydicom.Dataset) -> None:
        """
        Apply profile strategy to patient information fields.

        Never clear PatientAge, PatientSize, or PatientWeight as they are
        often needed for research and are not directly identifiable.
        Always clear other patient identifiers that are not explicitly handled
        by the profile to ensure no residual identifiable information remains.
        """
        # PatientName
        if "PatientName" in ds:
            new_value = self.apply_field_value_strategy(ds.PatientName, self.profile.PatientName)
            self.set_field(ds, "PatientName", new_value)
        
        # PatientID
        if "PatientID" in ds:
            original_id = str(ds.PatientID)
            new_value = self.apply_field_value_strategy(original_id, self.profile.PatientID)
            self.set_field(ds, "PatientID", new_value)
        
        # PatientBirthDate
        if "PatientBirthDate" in ds:
            new_value = self.apply_field_value_strategy(ds.PatientBirthDate, self.profile.PatientBirthDate)
            self.set_field(ds, "PatientBirthDate", new_value)

        # PatientAge
        if "PatientAge" in ds:
            new_value = self.apply_field_value_strategy(ds.PatientAge, self.profile.PatientAge)
            self.set_field(ds, "PatientAge", new_value)
        
        # PatientSex
        if "PatientSex" in ds:
            new_value = self.apply_field_value_strategy(ds.PatientSex, self.profile.PatientSex)
            self.set_field(ds, "PatientSex", new_value)
        
        # Always clear other patient identifiers
        if "OtherPatientIDs" in ds:
            ds.OtherPatientIDs = ""
        if "PatientAddress" in ds:
            ds.PatientAddress = ""
    
    def anonymize_study_series_fields(self, ds: pydicom.Dataset) -> None:
        """Apply profile strategy to study and series information fields."""
        # StudyInstanceUID with mapping
        if "StudyInstanceUID" in ds:
            original_uid = str(ds.StudyInstanceUID)
            new_uid = self.apply_uid_mapping(original_uid, self._study_uid_map, 
                                              self.profile.StudyInstanceUID)
            ds.StudyInstanceUID = new_uid
        
        # SeriesInstanceUID with mapping
        if "SeriesInstanceUID" in ds:
            original_uid = str(ds.SeriesInstanceUID)
            new_uid = self.apply_uid_mapping(original_uid, self._series_uid_map, 
                                              self.profile.SeriesInstanceUID)
            ds.SeriesInstanceUID = new_uid
        
        # FrameOfReferenceUID
        if "FrameOfReferenceUID" in ds:
            new_value = self.apply_field_value_strategy(ds.FrameOfReferenceUID, 
                                                         self.profile.FrameOfReferenceUID)
            if new_value:
                ds.FrameOfReferenceUID = new_value
            else:
                del ds.FrameOfReferenceUID
        
        # SOPInstanceUID - always generate new
        if "SOPInstanceUID" in ds:
            ds.SOPInstanceUID = self.generate_new_uid()
        
        # StudyID
        if "StudyID" in ds:
            new_value = self.apply_field_value_strategy(ds.StudyID, self.profile.StudyID)
            self.set_field(ds, "StudyID", new_value)
        
        # AccessionNumber
        if "AccessionNumber" in ds:
            new_value = self.apply_field_value_strategy(ds.AccessionNumber, 
                                                         self.profile.AccessionNumber)
            self.set_field(ds, "AccessionNumber", new_value)
        
        # StudyDescription
        if "StudyDescription" in ds:
            new_value = self.apply_field_value_strategy(ds.StudyDescription,
                                                         self.profile.StudyDescription)
            self.set_field(ds, "StudyDescription", new_value)

        #  SeriesDescription
        if "SeriesDescription" in ds:
            new_value = self.apply_field_value_strategy(ds.SeriesDescription,
                                                         self.profile.SeriesDescription)
            self.set_field(ds, "SeriesDescription", new_value)

    def anonymize_institution_physician_fields(self, ds: pydicom.Dataset) -> None:
        """Apply profile strategy to institution and physician information fields."""
        # InstitutionName
        if "InstitutionName" in ds:
            new_value = self.apply_field_value_strategy(ds.InstitutionName, 
                                                         self.profile.InstitutionName)
            self.set_field(ds, "InstitutionName", new_value)
        
        # ReferringPhysicianName
        if "ReferringPhysicianName" in ds:
            new_value = self.apply_field_value_strategy(ds.ReferringPhysicianName, 
                                                         self.profile.ReferringPhysicianName)
            self.set_field(ds, "ReferringPhysicianName", new_value)
        
        # OperatorsName
        if "OperatorsName" in ds:
            new_value = self.apply_field_value_strategy(ds.OperatorsName, 
                                                         self.profile.OperatorsName)
            self.set_field(ds, "OperatorsName", new_value)
        
        # PerformingPhysicianName
        if "PerformingPhysicianName" in ds:
            new_value = self.apply_field_value_strategy(ds.PerformingPhysicianName, 
                                                         self.profile.PerformingPhysicianName)
            self.set_field(ds, "PerformingPhysicianName", new_value)
        
        # InstitutionAddress - always clear
        if "InstitutionAddress" in ds:
            ds.InstitutionAddress = ""
    
    def anonymize_dates(self, ds: pydicom.Dataset) -> None:
        """Apply profile strategy to date/time fields."""
        current_date = self.current_date()
        current_time = self.current_time()
        
        # Instance creation dates - always anonymize
        if "InstanceCreationDate" in ds:
            ds.InstanceCreationDate = current_date
        if "InstanceCreationTime" in ds:
            ds.InstanceCreationTime = current_time
        
        # Content dates - always anonymize
        if "ContentDate" in ds:
            ds.ContentDate = current_date
        
        # Study dates - configurable per profile
        if not self.profile.RetainStudyDate:
            if "StudyDate" in ds:
                ds.StudyDate = current_date
            if "StudyTime" in ds:
                ds.StudyTime = current_time
        
        # Acquisition dates - always anonymize (only kept if RetainStudyDate is true)
        if not self.profile.RetainStudyDate:
            if "AcquisitionDate" in ds:
                ds.AcquisitionDate = current_date
            if "AcquisitionDateTime" in ds:
                ds.AcquisitionDateTime = current_date + current_time
            if "SeriesTime" in ds:
                ds.SeriesTime = current_time
    
    def remove_sensitive_tags(self, ds: pydicom.Dataset) -> None:
        """Remove highly sensitive DICOM tags."""
        tags_to_remove = [
            "OtherPatientIDsSequence", "PatientTelephoneNumbers", "MilitaryRank",
            "RequestAttributesSequence", "ClinicalTrialSponsorName", "ClinicalTrialProtocolID"
        ]
        for tag in tags_to_remove:
            if tag in ds:
                del ds[tag]
    
    def handle_private_tags(self, ds: pydicom.Dataset) -> None:
        """Remove private tags if configured."""
        if not self.profile.KeepPrivateTags:
            ds.remove_private_tags()
    
    def remove_overlays(self, ds: pydicom.Dataset) -> None:
        """Remove overlay data (60xx groups)."""
        for overlay_group in range(0x6000, 0x6020, 0x2):
            tags_to_delete = [tag for tag in ds.keys() if tag.group == overlay_group]
            for tag in tags_to_delete:
                del ds[tag]
    
    def handle_pixel_blackout(self, ds: pydicom.Dataset) -> None:
        """Blackout pixel data if configured."""
        if self.profile.PixelBlackout and "PixelData" in ds:
            try:
                # Set all pixel data to zeros
                ds.pixel_array[:] = 0
                # Update the PixelData element
                ds.PixelData = ds.pixel_array.tobytes()
            except Exception as e:
                logger.warning(f"Failed to blackout pixel data: {e}")
    
    def get_patient_id_mapping(self, original_patient_id: str) -> Optional[str]:
        """
        Get the anonymized Patient ID for an original Patient ID (if using PSEUDO).
        
        Args:
            original_patient_id (str): The original patient ID
            
        Returns:
            Optional[str]: The anonymized patient ID if mapped, None otherwise
        """
        original_str = str(original_patient_id)
        return self._pseudonym_map.get(original_str)
    
    def get_study_uid_mapping(self, original_study_uid: str) -> Optional[str]:
        """
        Get the anonymized Study Instance UID mapping.
        
        Args:
            original_study_uid (str): The original Study Instance UID
            
        Returns:
            Optional[str]: The anonymized Study Instance UID if mapped, None otherwise
        """
        return self._study_uid_map.get(str(original_study_uid))
    
    def get_series_uid_mapping(self, original_series_uid: str) -> Optional[str]:
        """
        Get the anonymized Series Instance UID mapping.
        
        Args:
            original_series_uid (str): The original Series Instance UID
            
        Returns:
            Optional[str]: The anonymized Series Instance UID if mapped, None otherwise
        """
        return self._series_uid_map.get(str(original_series_uid))
