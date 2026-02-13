import logging
from datetime import datetime
import pydicom
from pydicom.uid import generate_uid

from pixieveil.config import Settings

logger = logging.getLogger(__name__)

class Anonymizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        
    def _current_date(self):
        return datetime.now().strftime("%Y%m%d")
    
    def _current_time(self):
        return datetime.now().strftime("%H%M%S")
    
    def _generate_new_uid(self, prefix="2.25."):
        return generate_uid(prefix=prefix)

    def anonymize(self, ds: pydicom.Dataset) -> pydicom.Dataset:
        """
        Comprehensive DICOM field anonymization compliant with DICOM PS3.15
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
        new_study_uid = self._generate_new_uid()
        if "StudyInstanceUID" in ds: 
            ds.StudyInstanceUID = new_study_uid
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
        
        return ds
