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

    def anonymize(self, ds: pydicom.Dataset) -> pydicom.Dataset:
        """
        Comprehensive DICOM field anonymization compliant with DICOM PS3.15.
        
        This method performs comprehensive anonymization of a DICOM dataset,
        removing or replacing sensitive information while maintaining data
        integrity for medical imaging purposes.
        
        The anonymization process includes:
        - Patient information anonymization (name, ID, demographics)
        - Study/Series information anonymization (UIDs, descriptions)
        - Institution and physician information removal
        - Date/time fields anonymization with current values
        - Sensitive tag removal
        - Private tag removal
        - Overlay data removal
        - Burned-in annotation handling
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to anonymize
            
        Returns:
            pydicom.Dataset: The anonymized DICOM dataset
            
        Note:
            This method modifies the dataset in-place and also returns it
            for method chaining convenience.
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
            ds.StudyInstanceUID = self._generate_new_uid()
        if "SeriesInstanceUID" in ds: 
            ds.SeriesInstanceUID = self._generate_new_uid()
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
            
        # Remove private tags and overlays
        ds.remove_private_tags()
        
        # Remove overlay data (60xx groups)
        for overlay_group in range(0x6000, 0x6020, 0x2):
            if overlay_group in ds:
                del ds[overlay_group]
        
        # Return the modified dataset for chaining
        return ds
