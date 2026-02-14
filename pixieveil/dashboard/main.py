"""
Dashboard Web Interface Module

This module provides the web-based dashboard interface for the PixieVeil application.
It serves as the main user interface for monitoring system status, viewing metrics,
 and managing DICOM study processing.

Classes:
    Dashboard: Main dashboard class handling web server setup and route management
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Any

from aiohttp import web
from aiohttp_jinja2 import setup as setup_jinja2, template

from pixieveil.config import Settings
from pixieveil.dashboard.sse import ServerSentEvents

logger = logging.getLogger(__name__)


class Dashboard:
    """
    Main dashboard class handling web server setup and route management.
    
    This class provides the web-based dashboard interface for the PixieVeil application.
    It manages the HTTP server, handles web routes, and provides real-time updates
    through Server-Sent Events.
    
    The dashboard provides:
    - Main dashboard page with system status
    - Metrics page showing processing statistics
    - Status page with detailed system information
    - Real-time updates via SSE connections
    
    Attributes:
        settings (Settings): Application configuration settings
        app (web.Application): aiohttp web application instance
        runner (web.AppRunner): Application runner for the web server
        site (web.TCPSite): TCP site for serving the web application
        sse (ServerSentEvents): Handler for Server-Sent Events
    """
    
    def __init__(self, settings: Settings, storage_manager):
        """
        Initialize the Dashboard with application settings and storage manager.
        
        Args:
            settings: Application configuration settings containing HTTP server
                      configuration (IP address, port, etc.)
            storage_manager: Storage manager instance for accessing system metrics
        """
        self.settings = settings
        self.app = web.Application()
        self.runner = None
        self.site = None
        self.sse = ServerSentEvents()
        self.app['storage_manager'] = storage_manager

    async def start(self):
        """
        Start the dashboard web server.
        
        This method sets up the web application, registers routes,
        and starts the HTTP server. The dashboard becomes available
        at the configured IP address and port.
        
        The method:
        - Sets up all web routes
        - Creates the application runner and TCP site
        - Starts the web server
        - Logs the server address for user access
        
        Raises:
            Exception: If the web server fails to start
        """
        logger.info("Starting dashboard")

        # Setup Jinja2 templates
        setup_jinja2(self.app, loader=web.FileLoader(Path(__file__).parent / 'templates'))

        # Setup routes
        self.app.add_routes([
            web.get("/", self.handle_index),
            web.get("/metrics", self.handle_metrics),
            web.get("/status", self.handle_status),
            web.get("/events", self.sse.handle_events),
        ])

        # Create runner and site
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.settings.http_server["ip"], self.settings.http_server["port"])

        # Start the site
        await self.site.start()

        logger.info(f"Dashboard started on http://{self.settings.http_server['ip']}:{self.settings.http_server['port']}")

    async def stop(self):
        """
        Stop the dashboard web server gracefully.
        
        This method performs a graceful shutdown of the web server,
        including stopping the site and cleaning up the application runner.
        
        The method:
        - Stops the TCP site
        - Cleans up the application runner
        - Logs the shutdown completion
        """
        logger.info("Stopping dashboard")
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("Dashboard stopped")

    @template('index.html')
    async def handle_index(self, request: web.Request) -> Dict[str, Any]:
        """
        Handle the main dashboard page.
        
        This method serves the main dashboard page which provides an overview
        of the system status and navigation to other dashboard pages.
        
        The page includes:
        - System status display (updated via SSE)
        - Navigation links to metrics and status pages
        - Real-time status updates via JavaScript EventSource
        
        Args:
            request (web.Request): The incoming HTTP request
            
        Returns:
            Dict[str, Any]: Template context data
        """
        return {}

    @template('metrics.html')
    async def handle_metrics(self, request: web.Request) -> Dict[str, Any]:
        """
        Handle the metrics page.
        
        This method serves the metrics page which displays processing
        statistics and performance metrics.
        
        The page includes:
        - Number of studies processed
        - Number of images processed
        - Average processing time
        - Navigation back to the main dashboard
        
        Args:
            request (web.Request): The incoming HTTP request
            
        Returns:
            Dict[str, Any]: Template context data
        """
        storage_manager = request.app['storage_manager']
        metrics = {
            "studies_processed": storage_manager.completed_count,
            "images_processed": len(storage_manager.image_counters) if hasattr(storage_manager, 'image_counters') else 0,
            "average_processing_time": 0
        }
        return metrics

    @template('status.html')
    async def handle_status(self, request: web.Request) -> Dict[str, Any]:
        """
        Handle the status page.
        
        This method serves the status page which provides detailed
        information about the current system status.
        
        The page includes:
        - Server status (running/stopped)
        - Number of studies in progress
        - Total number of studies
        - Navigation back to the main dashboard
        
        Args:
            request (web.Request): The incoming HTTP request
            
        Returns:
            Dict[str, Any]: Template context data
        """
        storage_manager = request.app['storage_manager']
        status = {
            "server_status": "running",
            "studies_in_progress": len(storage_manager.study_states) if hasattr(storage_manager, 'study_states') else 0,
            "total_studies": storage_manager.completed_count + len(storage_manager.study_states) if hasattr(storage_manager, 'study_states') else storage_manager.completed_count
        }
        return status
