"""
Remote Storage Module

This module provides functionality for uploading DICOM study archives to remote storage services.
It supports configurable remote storage endpoints with authentication.

Classes:
    RemoteStorage: Handles remote storage operations for DICOM study archives
"""

import logging
import aiohttp
from pathlib import Path
from typing import Dict, Any, Optional

from pixieveil.config import Settings

logger = logging.getLogger(__name__)


class RemoteStorage:
    """
    Handles remote storage operations for DICOM study archives.
    
    This class provides functionality to upload DICOM study ZIP archives to
    a remote storage service. It supports configurable endpoints and
    authentication mechanisms.
    
    Attributes:
        settings (Settings): Application configuration settings
        base_url (str): Base URL for the remote storage service
        auth_token (str): Authentication token for remote storage access
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize the RemoteStorage with application settings.
        
        Args:
            settings: Application configuration settings containing remote storage
                      configuration including base URL and authentication token
        """
        self.settings = settings
        self.base_url = settings.storage.get("remote_storage", {}).get("base_url")
        self.auth_token = settings.storage.get("remote_storage", {}).get("auth_token")

    async def upload_file(self, file_path: Path, remote_path: str) -> bool:
        """
        Upload a file to remote storage.
        
        This method uploads a DICOM study ZIP archive to the configured remote storage service.
        It handles authentication and error reporting for the upload operation.
        
        Args:
            file_path (Path): Path to the local file to upload
            remote_path (str): Remote path where the file should be stored
            
        Returns:
            bool: True if upload was successful, False if upload failed,
                  None if remote storage is not configured
                  
        Note:
            If remote storage is not configured (base_url is None), the method
            returns None to indicate that remote storage is disabled.
        """
        try:
            if not self.base_url:
                logger.warning("Remote storage not configured")
                return None

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/upload",
                    data={"file": open(file_path, "rb")},
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                    json={"remote_path": remote_path}
                ) as response:
                    if response.status == 200:
                        logger.info(f"Successfully uploaded {file_path} to {remote_path}")
                        return True
                    else:
                        logger.error(f"Failed to upload {file_path}: {response.status}")
                        return False

        except Exception as e:
            logger.error(f"Error uploading {file_path}: {e}")
            return False
