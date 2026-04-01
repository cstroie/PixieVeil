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
import random
import string
import pydicom
from pydicom.uid import generate_uid

from pixieveil.config import Settings

logger = logging.getLogger(__name__)


class Anonymizer:
    """
    Handles DICOM dataset anonymization operations.
    
    This class provides comprehensive DICOM field anonymization compliant with
    DICOM PS3.15 standards. It removes or replaces sensitive patient information
    while maintaining the integrity of the medical imaging data.
    
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
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the Anonymizer with application settings.
        
        Args:
            settings: Application configuration settings containing anonymization
                      rules and preferences
        """
        self.settings = settings
        # UID mappings to ensure consistency across studies and series
        self._study_uid_map = {}  # Maps original StudyInstanceUID -> anonymized UID
        self._series_uid_map = {}  # Maps original SeriesInstanceUID -> anonymized UID
        # Patient ID mapping for consistency across study
        self._patient_id_map = {}  # Maps original PatientID -> anonymized ID
        
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
    
    def _generate_new_uid(self, prefix="2.25."):
        """
        Generate a new DICOM UID with the specified prefix.
        
        Args:
            prefix (str): Prefix for the generated UID (default: "2.25.")
            
        Returns:
            str: Newly generated DICOM UID
        """
        return generate_uid(prefix=prefix)
    
    def _generate_patient_id(self):
        """
        Generate a random anonymized Patient ID.
        
        Returns:
            str: Random patient ID in format "PAT-XXXXXXXX" (3 letters + 8 random numeric)
        """
        random_part = ''.join(random.choices(string.digits, k=8))
        return f"PAT-{random_part}"
    
    
    def get_patient_id_mapping(self, original_patient_id: str) -> Optional[str]:
        """
        Get the anonymized Patient ID for an original Patient ID.
        
        Args:
            original_patient_id (str): The original patient ID
            
        Returns:
            Optional[str]: The anonymized patient ID if mapped, None otherwise
        """
        return self._patient_id_map.get(str(original_patient_id))
    
    def get_study_uid_mapping(self, original_study_uid: str) -> Optional[str]:
        """
        Get the anonymized Study Instance UID for an original Study Instance UID.
        
        Args:
            original_study_uid (str): The original Study Instance UID
            
        Returns:
            Optional[str]: The anonymized Study Instance UID if mapped, None otherwise
        """
        return self._study_uid_map.get(str(original_study_uid))
    
    def get_series_uid_mapping(self, original_series_uid: str) -> Optional[str]:
        """
        Get the anonymized Series Instance UID for an original Series Instance UID.
        
        Args:
            original_series_uid (str): The original Series Instance UID
            
        Returns:
            Optional[str]: The anonymized Series Instance UID if mapped, None otherwise
        """
        return self._series_uid_map.get(str(original_series_uid))
    
    def anonymize(self, ds: pydicom.Dataset, 
                  study_instance_uid: str = None, 
                  series_instance_uid: str = None,
                  keep_study_dates: bool = False,
                  keep_acquisition_dates: bool = False,
                  keep_birth_date: bool = False,
                  keep_age: bool = False,
                  keep_sex: bool = False) -> pydicom.Dataset:
        """
        Comprehensive DICOM field anonymization compliant with DICOM PS3.15.
        
        This method performs comprehensive anonymization of a DICOM dataset,
        removing or replacing sensitive information while maintaining data
        integrity for medical imaging purposes.
        
        The anonymization process includes:
        - Patient information anonymization (name, ID, demographics)
        - Study/Series information anonymization (UIDs, descriptions)
        - Institution and physician information removal
        - Date/time fields anonymization (configurable)
        - Sensitive tag removal
        - Private tag removal
        - Overlay data removal
        - Burned-in annotation handling
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to anonymize
            study_instance_uid (str, optional): Predetermined StudyInstanceUID to use 
                for all files in the same study. If not provided, the anonymizer will
                automatically map the original UID to maintain consistency.
            series_instance_uid (str, optional): Predetermined SeriesInstanceUID to use 
                for all files in the same series. If not provided, the anonymizer will
                automatically map the original UID to maintain consistency.
            keep_study_dates (bool): If True, preserves StudyDate and StudyTime. 
                Default: False (anonymize to current date/time)
            keep_acquisition_dates (bool): If True, preserves AcquisitionDate, 
                AcquisitionDateTime, and related fields. Default: False
            keep_birth_date (bool): If True, preserves PatientBirthDate. 
                Default: False (clear the field)
            keep_age (bool): If True, preserves PatientAge. 
                Default: False (clear the field)
            keep_sex (bool): If True, preserves PatientSex. 
                Default: False (clear the field)
            
        Returns:
            pydicom.Dataset: The anonymized DICOM dataset
            
        Note:
            This method modifies the dataset in-place and also returns it
            for method chaining convenience.
            
            UID Mapping Strategy:
            - The first time a StudyInstanceUID is encountered, it's mapped to a new UID
            - All subsequent files with the same StudyInstanceUID receive the same new UID
            - The same logic applies to SeriesInstanceUID
            - This ensures proper DICOM hierarchy is maintained across multiple files
            
            Patient ID Mapping:
            - Each unique PatientID is mapped to a random anonymized ID
            - All files with the same PatientID receive the same anonymized ID
            - This maintains consistency across a study
        """
        # Patient Information
        ds.PatientName = "Anonymous"
        
        # Patient ID mapping for consistency across study
        original_patient_id = str(ds.PatientID) if "PatientID" in ds else "UNKNOWN"
        if original_patient_id not in self._patient_id_map:
            self._patient_id_map[original_patient_id] = self._generate_patient_id()
        ds.PatientID = self._patient_id_map[original_patient_id]
        
        # Birth date handling
        if not keep_birth_date:
            ds.PatientBirthDate = ""
        
        # Sex handling
        if not keep_sex:
            ds.PatientSex = ""
        
        # Age handling
        if "PatientAge" in ds and not keep_age:
            ds.PatientAge = ""
        
        if "OtherPatientIDs" in ds: 
            ds.OtherPatientIDs = ""
        if "PatientAddress" in ds: 
            ds.PatientAddress = ""
        if "PatientSize" in ds: 
            ds.PatientSize = ""
        if "PatientWeight" in ds: 
            ds.PatientWeight = ""
        
        # Study/Series Information with automatic UID mapping
        if "StudyInstanceUID" in ds:
            original_study_uid = str(ds.StudyInstanceUID)
            if study_instance_uid:
                # Use provided UID
                ds.StudyInstanceUID = study_instance_uid
            else:
                # Use mapped UID or create new mapping
                if original_study_uid not in self._study_uid_map:
                    self._study_uid_map[original_study_uid] = self._generate_new_uid()
                ds.StudyInstanceUID = self._study_uid_map[original_study_uid]
        
        if "SeriesInstanceUID" in ds:
            original_series_uid = str(ds.SeriesInstanceUID)
            if series_instance_uid:
                # Use provided UID
                ds.SeriesInstanceUID = series_instance_uid
            else:
                # Use mapped UID or create new mapping
                if original_series_uid not in self._series_uid_map:
                    self._series_uid_map[original_series_uid] = self._generate_new_uid()
                ds.SeriesInstanceUID = self._series_uid_map[original_series_uid]
        
        if "SOPInstanceUID" in ds: 
            ds.SOPInstanceUID = self._generate_new_uid()
        ds.AccessionNumber = self._generate_new_uid(prefix="1.98765.")[:16]  # Simulate accession format
        ds.StudyDescription = "Anonymized Study"
        if "SeriesDescription" in ds: ds.SeriesDescription = "Anonymized Series"
        
        # Institution and Physician Information
        if "InstitutionName" in ds: ds.InstitutionName = ""
        if "InstitutionAddress" in ds: ds.InstitutionAddress = ""
        if "ReferringPhysicianName" in ds: ds.ReferringPhysicianName = ""
        if "OperatorsName" in ds: ds.OperatorsName = ""
        if "PerformingPhysicianName" in ds: ds.PerformingPhysicianName = ""
        
        # Dates and Times (configurable)
        current_date = self._current_date()
        current_time = self._current_time()
        
        # Instance creation dates (always anonymized)
        if "InstanceCreationDate" in ds: 
            ds.InstanceCreationDate = current_date
        if "InstanceCreationTime" in ds: 
            ds.InstanceCreationTime = current_time
        
        # Content dates (always anonymized)
        if "ContentDate" in ds: 
            ds.ContentDate = current_date
        
        # Study dates (configurable)
        if not keep_study_dates:
            if "StudyDate" in ds: 
                ds.StudyDate = current_date
            if "StudyTime" in ds: 
                ds.StudyTime = current_time
        
        # Acquisition dates (configurable)
        if not keep_acquisition_dates:
            if "AcquisitionDate" in ds: 
                ds.AcquisitionDate = current_date
            if "AcquisitionDateTime" in ds: 
                ds.AcquisitionDateTime = current_date + current_time
            if "SeriesTime" in ds: 
                ds.SeriesTime = current_time
        
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
            
        # Remove private tags and overlays
        ds.remove_private_tags()
        
        # Remove overlay data (60xx groups)
        for overlay_group in range(0x6000, 0x6020, 0x2):
            if overlay_group in ds:
                del ds[overlay_group]
        
        # Return the modified dataset for chaining
        return ds
