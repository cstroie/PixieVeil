"""
DICOM Series Filter Module

This module provides functionality for filtering DICOM series based on
configurable criteria, such as modality type and series characteristics.

Classes:
    SeriesFilter: Handles DICOM series filtering operations
"""

import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple

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
        only_original_series (bool): Whether to keep only original series
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
        self.only_original_series = settings.series_filter.get("only_original_series", True)

        self._include_rules: List[Tuple[str, re.Pattern]] = self.compile_rules(
            settings.series_filter.get("include") or {}
        )
        self._exclude_rules: List[Tuple[str, re.Pattern]] = self.compile_rules(
            settings.series_filter.get("exclude") or {}
        )

    @staticmethod
    def compile_rules(rules: Dict[str, str]) -> List[Tuple[str, re.Pattern]]:
        compiled = []
        for attribute, pattern in rules.items():
            try:
                compiled.append((attribute, re.compile(pattern)))
            except re.error as exc:
                logger.warning(f"Invalid regex pattern '{pattern}' for attribute '{attribute}': {exc}")
        return compiled

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
            if "Modality" in ds and ds.Modality in self.exclude_modalities:
                logger.debug(f"Filtering out image with excluded modality: {ds.Modality}")
                return True

            # Check if we should keep only original series
            if self.only_original_series:
                if not self.is_original_series(ds):
                    logger.debug(f"Filtering out non-original series: {ds.SeriesInstanceUID}")
                    return True

            # Attribute-based include/exclude rules
            if self._include_rules or self._exclude_rules:
                if self.matches_attribute_filters(ds):
                    return True

            return False

        except Exception as e:
            logger.error(f"Error in series filtering: {e}")
            return False

    def is_original_series(self, ds: pydicom.Dataset) -> bool:
        """
        Determine if a series is an original series (not a reconstruction).
        
        This method evaluates whether a DICOM series represents an original
        acquisition rather than a reconstructed or derived series.
        
        Args:
            ds (pydicom.Dataset): The DICOM dataset to evaluate
            
        Returns:
            bool: True if the series is considered original, False otherwise
            
        Note:
            If the ImageType attribute is absent, the series is assumed to be
            original. Series with ImageType starting with 'DERIVED' (e.g.
            reconstructions) return False.
        """
        image_type = getattr(ds, "ImageType", None)
        if image_type is None:
            return True
        # ImageType is a multi-value CS attribute; first value indicates original vs derived
        first_value = image_type[0] if hasattr(image_type, "__iter__") else str(image_type)
        return str(first_value).upper().startswith("ORIGINAL")

    def matches_attribute_filters(self, ds: pydicom.Dataset) -> bool:
        """
        Apply attribute-based include/exclude rules to determine if a series should
        be filtered out.

        Include rules take priority: if any include rule matches the dataset, the
        series is kept regardless of exclude rules.  If no include rule matches but
        an exclude rule does, the series is filtered out.

        Multi-value DICOM attributes (e.g. ImageType) are tested value-by-value;
        a rule matches as soon as any individual value satisfies the pattern.

        Args:
            ds: The DICOM dataset to evaluate.

        Returns:
            bool: True if the series should be filtered out, False if it should be kept.
        """
        def attribute_matches(rules: List[Tuple[str, re.Pattern]]) -> bool:
            for attribute, pattern in rules:
                value = getattr(ds, attribute, None)
                if value is None:
                    continue
                values = list(value) if hasattr(value, "__iter__") and not isinstance(value, str) else [str(value)]
                if any(pattern.search(str(v)) for v in values):
                    logger.debug(f"Attribute filter matched: {attribute} ~ {pattern.pattern}")
                    return True
            return False

        if self._include_rules and attribute_matches(self._include_rules):
            return False  # include match → keep

        if attribute_matches(self._exclude_rules):
            return True  # exclude match → filter out

        return False  # no rule matched → keep
