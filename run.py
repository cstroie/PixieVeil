# Start services
await asyncio.gather(
    dicom_server.start(),
    dashboard.start()
)
