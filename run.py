import asyncio
import logging
from pixieveil.config import Settings
from pixieveil.dicom_server.server import DicomServer
from pixieveil.dashboard.main import Dashboard

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
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
    
    # Start services
    await asyncio.gather(
        dicom_server.start(),
        dashboard.start()
    )

if __name__ == "__main__":
    asyncio.run(main())
