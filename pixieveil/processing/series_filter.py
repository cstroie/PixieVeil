import logging
from pathlib import Path
from typing import Dict, Any, List

import pydicom

from pixieveil.config import Settings

logger = logging.getLogger(__name__)

class SeriesFilter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.exclude_modalities = settings.series_filter.get("exclude_modalities", [])
        self.keep_original_series = settings.series_filter.get("keep_original_series", True)

    def should_filter(self, ds: pydicom.Dataset) -> bool:
        """
        Determine if a DICOM image should be filtered based on series criteria.
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
        """
        # Basic implementation - can be enhanced
        # For now, assume all series are original
        return True
