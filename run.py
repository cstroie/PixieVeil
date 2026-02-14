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
from pixieveil.config import Settings
from pixieveil.dicom_server.server import DicomServer
from pixieveil.dashboard.main import Dashboard
from pixieveil.storage.storage_manager import StorageManager

# Configure logging
def setup_logging(settings):
    """
    Configure application logging based on settings.
    
    This function sets up the Python logging system with the configuration
    specified in the application settings, including log level, format,
    and output handlers.
    
    Args:
        settings (Settings): Application configuration settings containing
                            logging configuration
    """
    logging.basicConfig(
        level=settings.logging.get("level", "INFO"),
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('pixieveil.log')
        ]
    )

logger = logging.getLogger(__name__)

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
    logger.info("Starting PixieVeil application")
    
    # Load settings
    settings = Settings.load()
    
    # Setup logging with settings
    setup_logging(settings)
    
    # Create service instances
    storage_manager = StorageManager(settings)
    dicom_server = DicomServer(settings, storage_manager)
    dashboard = Dashboard(settings, storage_manager)

    try:
        # Start services - wrap the storage_manager call in a task
        await asyncio.gather(
            dicom_server.start(),
            dashboard.start(),
            asyncio.create_task(storage_manager.check_study_completions())
        )
    except asyncio.CancelledError:
        logger.info("Shutting down services...")
        await asyncio.gather(
            dicom_server.stop(),
            dashboard.stop(),
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application shutdown by user")
