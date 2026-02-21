"""
DICOM Processing Pipeline Module

This module provides a comprehensive processing pipeline for DICOM images,
including validation, filtering, anonymization, and study management.

Classes:
    ProcessingPipeline: Orchestrates the complete DICOM image processing workflow
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings
from pixieveil.processing.anonymizer import Anonymizer
from pixieveil.processing.series_filter import SeriesFilter
from pixieveil.processing.study_manager import StudyManager

logger = logging.getLogger(__name__)


class ProcessingPipeline:
    """
    Orchestrates the complete DICOM image processing workflow.
    
    This class provides a comprehensive processing pipeline for DICOM images,
    coordinating multiple processing steps including validation, filtering,
    anonymization, and study management.
    
    The pipeline processes images through the following stages:
    1. DICOM validation
    2. Series filtering (based on configured criteria)
    3. Anonymization (removal of sensitive information using configurable profiles)
    4. Study management (tracking and completion monitoring)
    
    Attributes:
        settings (Settings): Application configuration settings
        anonymizer (Anonymizer): Handler for DICOM anonymization
        series_filter (SeriesFilter): Handler for series filtering
        study_manager (StudyManager): Handler for study management
        anonymization_profile (Optional[str]): Current anonymization profile name
    """
    
    def __init__(self, settings: Settings, anonymization_profile: Optional[str] = None):
        """
        Initialize the ProcessingPipeline with application settings and anonymization profile.
        
        Args:
            settings: Application configuration settings containing processing
                      pipeline configuration and component settings
            anonymization_profile: Name of the anonymization profile to use (optional)
        """
        self.settings = settings
        self.anonymization_profile = anonymization_profile
        self.anonymizer = Anonymizer(settings, anonymization_profile)
        self.series_filter = SeriesFilter(settings)
        self.study_manager = StudyManager(settings)

    async def process_image(self, image_path: Path, image_id: str) -> Optional[Path]:
        """
        Process a received DICOM image through the complete pipeline.
        
        This method orchestrates the complete processing workflow for a DICOM image,
        including validation, filtering, anonymization, and study management.
        
        The processing pipeline consists of the following steps:
        1. DICOM dataset validation
        2. Series filtering based on configured criteria
        3. Anonymization of sensitive information using the configured profile
        4. Study management and tracking
        
        Args:
            image_path (Path): Path to the DICOM file to process
            image_id (str): Unique identifier for this DICOM image
            
        Returns:
            Optional[Path]: Path to the processed DICOM file if successful,
                          None if processing failed or image was filtered out
                          
        Note:
            If any step in the pipeline fails, the method will return None
            and log the error. The pipeline is designed to be fault-tolerant
            and will continue processing subsequent images even if one fails.
        """
        try:
            # Read the DICOM image
            ds = pydicom.dcmread(image_path)

            # Validate the image
            if not self._validate_dicom(ds):
                logger.warning(f"Invalid DICOM image: {image_id}")
                return None

            # Check if image should be filtered
            if self.series_filter.should_filter(ds):
                logger.info(f"Filtering out image {image_id} based on series criteria")
                return None

            # Anonymize the image using the configured profile
            anonymized_path = await self.anonymizer.anonymize(ds, image_path, image_id)
            if not anonymized_path:
                logger.warning(f"Failed to anonymize image: {image_id}")
                return None

            # Process study management
            await self.study_manager.process_image(anonymized_path, image_id)

            logger.info(f"Successfully processed image {image_id} using profile '{self.anonymization_profile}'")
            return anonymized_path

        except Exception as e:
            logger.error(f"Failed to process image {image_id}: {e}")
            return None

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
        # Basic validation
        required_fields = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"]
        for field in required_fields:
            if not hasattr(ds, field):
                return False

        return True
