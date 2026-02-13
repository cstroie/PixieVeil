import asyncio
import logging
import aiohttp
from pathlib import Path
from typing import Dict, Any, Optional

from pixieveil.config import Settings

logger = logging.getLogger(__name__)

class RemoteStorage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.storage.get("remote_storage", {}).get("base_url")
        self.auth_token = settings.storage.get("remote_storage", {}).get("auth_token")

    async def upload_file(self, file_path: Path, remote_path: str) -> bool:
        """
        Upload a file to remote storage.
        """
        try:
            if not self.base_url:
                logger.warning("Remote storage not configured")
                return False

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
