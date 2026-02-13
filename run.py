import asyncio
import logging
from pathlib import Path

from pixieveil.config import Settings
from pixieveil.dicom_server import DicomServer
from pixieveil.dashboard import Dashboard
from pixieveil.utils.logger import setup_logging

async def main():
    # Setup logging
    setup_logging()

    # Load configuration
    settings = Settings.load()

    # Initialize components
    dicom_server = DicomServer(settings)
    dashboard = Dashboard(settings)

    # Start services
    await asyncio.gather(
        dicom_server.start(),
        dashboard.start()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("PixieVeil server stopped by user")
    except Exception as e:
        logging.error(f"Failed to start PixieVeil: {e}")
        raise
