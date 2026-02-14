import asyncio
import logging
from pixieveil.config import Settings
from pixieveil.dicom_server.server import DicomServer
from pixieveil.dashboard.main import Dashboard
from pixieveil.storage.storage_manager import StorageManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('pixieveil.log')
    ]
)

logger = logging.getLogger(__name__)

async def main():
    logger.info("Starting PixieVeil application")
    
    # Load settings
    settings = Settings.load()
    
    # Create service instances
    dicom_server = DicomServer(settings)
    storage_manager = StorageManager(settings)
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
