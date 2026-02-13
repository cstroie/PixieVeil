import asyncio
import logging
from pixieveil.config import Settings
from pixieveil.dicom_server.server import DicomServer
from pixieveil.dashboard.main import Dashboard

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S.%f',
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
    dashboard = Dashboard(settings)
    storage_manager = StorageManager(settings)

    try:
        # Start services
        await asyncio.gather(
            dicom_server.start(),
            dashboard.start(),
            storage_manager.check_study_completions()
        )
    except asyncio.CancelledError:
        logger.info("Shutting down services...")
        await asyncio.gather(
            dicom_server.stop(),
            dashboard.stop()
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application shutdown by user")
