import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

import pydicom

from pixieveil.config import Settings

logger = logging.getLogger(__name__)

class Anonymizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def anonymize(self, ds: pydicom.Dataset, image_path: Path, image_id: str) -> Optional[Path]:
        """
        Anonymize the DICOM image using the dicom_anonymization project.
        """
        try:
            # Create output directory
            output_dir = image_path.parent / "anonymized"
            output_dir.mkdir(exist_ok=True)

            # Build command
            command = [
                "python", "-m", "dicom_anonymization.anony",
                "-i", str(image_path),
                "-o", str(output_dir),
                "-p", self.settings.anonymization["profile"],
                "--salt", self.settings.anonymization["salt"]
            ]

            # Run anonymization
            result = subprocess.run(command, capture_output=True, text=True)

            if result.returncode != 0:
                logger.error(f"Anonymization failed for image {image_id}: {result.stderr}")
                return None

            # Find the anonymized file
            anonymized_file = next(output_dir.glob("*.dcm"))
            return anonymized_file

        except Exception as e:
            logger.error(f"Failed to anonymize image {image_id}: {e}")
            return None
