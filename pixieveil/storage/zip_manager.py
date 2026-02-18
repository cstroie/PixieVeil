"""
ZIP Manager Module

This module provides functionality for creating ZIP archives of DICOM studies.
It handles the compression and packaging of DICOM study directories for
storage and transfer purposes.

Classes:
    ZipManager: Handles ZIP archive creation for DICOM studies
"""

import logging
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional

from pixieveil.config import Settings

logger = logging.getLogger(__name__)


class ZipManager:
    """
    Handles ZIP archive creation for DICOM studies.
    
    This class provides functionality to create compressed ZIP archives of
    DICOM study directories. It recursively includes all files in the study
    directory and maintains the directory structure within the archive.
    
    Attributes:
        settings (Settings): Application configuration settings containing
                            storage path configuration
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the ZipManager with application settings.
        
        Args:
            settings: Application configuration settings containing storage
                      path configuration for finding study directories
        """
        self.settings = settings

    async def create_zip(self, study_uid: str, output_path: Path) -> Optional[Path]:
        """
        Create a zip file for a study.
        
        This method creates a compressed ZIP archive of a DICOM study directory.
        It recursively includes all files in the study directory and maintains
        the directory structure within the archive.
        
        Args:
            study_uid (str): The study identifier (typically numeric) to archive
            output_path (Path): Path where the ZIP archive should be created
            
        Returns:
            Optional[Path]: Path to the created ZIP archive if successful,
                          None if the operation failed
                          
        Note:
            The method expects the study directory to exist at the configured
            base path with the specified study UID. If the directory doesn't
            exist or cannot be accessed, the method will return None.
        """
        try:
            # Get study directory
            study_dir = Path(self.settings.storage["base_path"]) / study_uid

            # Create zip file
            zip_path = output_path / f"{study_uid}.zip"
            with zipfile.ZipFile(zip_path, "w") as zipf:
                for file_path in study_dir.rglob("*"):
                    if file_path.is_file():
                        arcname = file_path.relative_to(study_dir)
                        zipf.write(file_path, arcname)

            logger.info(f"Created zip file for study {study_uid}: {zip_path}")
            return zip_path

        except Exception as e:
            logger.error(f"Failed to create zip for study {study_uid}: {e}")
            return None
