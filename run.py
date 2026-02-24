"""
PixieVeil Application Entry Point

This module serves as the main entry point for the PixieVeil application.
It initializes all services, sets up logging, and coordinates the startup
and shutdown of all application components.

Functions:
    setup_logging: Configure application logging based on settings
    main: Main application entry point that starts all services
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from logging.handlers import RotatingFileHandler
from pixieveil.config import Settings
from pixieveil.dicom_server.server import DicomServer
from pixieveil.dashboard.server import Dashboard
from pixieveil.storage.storage_manager import StorageManager

# Configure logging
def setup_logging(settings: Settings) -> None:
    """Configure application logging based on settings."""
    log_cfg = settings.logging
    log_path = Path(log_cfg.get("file", "pixieveil.log"))

    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            filename=log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ]

    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=handlers,
    )


async def main():
    """
    Main application entry point.
    
    This function initializes all application services, starts them,
    and manages their lifecycle. The services started include:
    - DICOM server for receiving DICOM images
    - Dashboard web interface
    - Storage manager for processing and archiving images
    
    The function runs all services concurrently and handles graceful
    shutdown when interrupted.
    """
    # Load settings
    settings = Settings.load()
    
    # Setup logging with settings
    setup_logging(settings)
    
    # Get module logger after logging is configured
    logger = logging.getLogger(__name__)
    logger.info("Starting PixieVeil application")
    
    # Create service instances
    storage_manager = StorageManager(settings)
    dicom_server = DicomServer(settings, storage_manager)
    dashboard = Dashboard(settings, storage_manager)

    try:
        # Start the background study‑completion checker first
        await storage_manager.start()

        # Run the remaining services concurrently
        await asyncio.gather(
            dashboard.start(),
            dicom_server.start(),
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutdown requested by user / cancellation")
    except Exception as exc:
        logger.exception("Unexpected error – shutting down")
    finally:
        logger.info("Stopping services...")
        await asyncio.gather(
            dicom_server.stop(),
            dashboard.stop(),
            return_exceptions=True,
        )
        await storage_manager.stop()

def _run() -> None:
    """Entry‑point used when executing ``run.py`` directly."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Minimal fallback logger in case configuration failed
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).info("Application shutdown by user")


if __name__ == "__main__":
    _run()
