"""
DICOM Series Filter Module

This module provides functionality for filtering DICOM series based on
configurable criteria, such as modality type and series characteristics.

Classes:
    SeriesFilter: Handles DICOM series filtering operations
"""

import logging
from pathlib import Path
from typing import Dict, Any, List

import pydicom

from pixieveil.config import Settings

logger = logging.getLogger(__name__)


class SeriesFilter:
    """
    Handles DICOM series filtering operations.
    
    This class provides functionality to filter DICOM series based on
    configurable criteria. It can exclude specific modalities and
    filter based on series characteristics.
    
    The filtering process includes:
    - Modality-based filtering (exclude specific modalities)
    - Series type filtering (original vs. reconstructed series)
    - Configurable filtering rules
    
    Attributes:
        settings (Settings): Application configuration settings
        exclude_modalities (List[str]): List of modalities to exclude
        keep_original_series (bool): Whether to keep only original series
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the SeriesFilter with application settings.
        
        Args:
            settings: Application configuration settings containing series filter
                      configuration including excluded modalities and series
                      type preferences
        """
        self.settings = settings
        self.exclude_modalities = settings.series_filter.get("exclude_modalities", [])
        self.keep_original_series = settings.series_filter.get("keep_original_series", True)

    def should_filter(self, ds: pydicom.Dataset) -> bool:
        """
        Determine if a DICOM image should be filtered based on series criteria.
        
        This method evaluates a DICOM dataset against configured filtering
        criteria to determine if it should be excluded from processing.
        
        The filtering criteria include:
        - Modality exclusion (if the modality is in the exclude list)
        - Series type filtering (if only original series should be kept)
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to evaluate for filtering
            
        Returns:
            bool: True if the image should be filtered out, False if it should
                  be processed further
                  
        Note:
            If an error occurs during filtering evaluation, the method will
            log the error and return False (allowing the image to be processed
            rather than potentially losing valid data).
        """
        try:
            # Check modality
            if ds.Modality in self.exclude_modalities:
                logger.debug(f"Filtering out image with excluded modality: {ds.Modality}")
                return True

            # Check if we should keep only original series
            if self.keep_original_series:
                if not self._is_original_series(ds):
                    logger.debug(f"Filtering out non-original series: {ds.SeriesInstanceUID}")
                    return True

            return False

        except Exception as e:
            logger.error(f"Error in series filtering: {e}")
            return False

    def _is_original_series(self, ds: pydicom.Dataset) -> bool:
        """
        Determine if a series is an original series (not a reconstruction).
        
        This method evaluates whether a DICOM series represents an original
        acquisition rather than a reconstructed or derived series.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to evaluate
            
        Returns:
            bool: True if the series is considered original, False otherwise
            
        Note:
            The current implementation assumes all series are original.
            This method can be enhanced with more sophisticated logic to
            distinguish between original and reconstructed series based
            on DICOM tags or other characteristics.
        """
        # Basic implementation - can be enhanced
        # For now, assume all series are original
        return True
