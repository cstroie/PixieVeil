import asyncio
from pixieveil.config import Settings
from pixieveil.dicom_server.server import DicomServer
from pixieveil.dashboard.main import Dashboard

async def main():
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
