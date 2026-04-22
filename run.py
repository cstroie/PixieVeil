#!/usr/bin/env python3.12

"""
PixieVeil Application Entry Point
=================================

This module is the executable entry point for the **PixieVeil** DICOM
processing service. It is responsible for:

* Loading the application configuration via :class:`pixieveil.config.settings.Settings`.
* Initialising structured, rotating‑file logging based on the configuration.
* Instantiating core services – the :class:`~pixieveil.storage.storage_manager.StorageManager`,
  the :class:`~pixieveil.dicom_server.server.DicomServer`, and the
  :class:`~pixieveil.dashboard.server.Dashboard`.
* Starting the services concurrently and handling graceful shutdown on
  cancellation, keyboard interrupt, or unexpected errors.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any
from logging.handlers import RotatingFileHandler
from pixieveil.config import Settings
from pixieveil.dicom_server.server import DicomServer
from pixieveil.dashboard.server import Dashboard
from pixieveil.storage.storage_manager import StorageManager

# Configure logging
def setup_logging(settings: Settings) -> None:
    """
    Configure the root logger according to the ``logging`` section of the
    :class:`~pixieveil.config.settings.Settings` instance.

    The function creates a rotating file handler (default 5 MiB per file,
    keeping three backups) and a stream handler that writes to ``stderr``.
    Log level, format and the log file path are all driven by the user‑
    supplied configuration, allowing the application to be customised
    without code changes.
    """
    log_cfg = settings.logging
    log_path = Path(log_cfg.get("file", "pixieveil.log"))
    # Ensure the directory for the log file exists to avoid FileNotFoundError
    log_path.parent.mkdir(parents=True, exist_ok=True)

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
        force=True,
    )


async def main() -> None:
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

    # Initialize task variables to None to prevent NameError in finally block
    dashboard_task = None
    dicom_task = None

    try:
        # Start the background study‑completion checker first
        await storage_manager.start()

        # Start services as background tasks
        dashboard_task = asyncio.create_task(dashboard.start())
        dicom_task = asyncio.create_task(dicom_server.start())

        # Wait indefinitely until cancelled (e.g., Ctrl‑C)
        await asyncio.Event().wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutdown requested by user / cancellation")
    except Exception as exc:
        logger.exception("Unexpected error – shutting down")
    finally:
        logger.info("Stopping services...")
        # Cancel running service tasks if they are still active
        for task in (dashboard_task, dicom_task):
            if task and not task.done():
                task.cancel()
        
        # Wait for services to stop with timeouts to prevent hanging
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    dicom_server.stop(),
                    dashboard.stop(),
                    return_exceptions=True
                ),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.error("Failed to stop services within 10 second timeout")
        
        # Stop storage manager with timeout
        try:
            await asyncio.wait_for(storage_manager.stop(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("Storage manager did not stop within 5 second timeout")

if __name__ == "__main__":
    """
    Entry‑point used when executing ``run.py`` directly.

    It wraps :func:`asyncio.run` around :func:`main` and provides a minimal
    fallback logger if the configuration cannot be loaded before the
    ``KeyboardInterrupt`` is caught.  This ensures a clean shutdown message
    is always emitted, even in error scenarios.
    """
    _settings = Settings.load()
    if _settings.defacing.get("enabled", False):
        if sys.version_info < (3, 12):
            sys.exit(
                f"Defacing requires Python >= 3.12 (running {sys.version.split()[0]})"
            )
        try:
            import torch
            print(f"Torch: {torch.__version__}")
        except ImportError:
            sys.exit("Defacing requires PyTorch — install it and retry.")
        try:
            import nnunetv2
            print("nnUNetv2 OK")
        except ImportError:
            sys.exit("Defacing requires nnUNetv2 — install it and retry.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Minimal fallback logger in case configuration failed
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).info("Application shutdown by user")
