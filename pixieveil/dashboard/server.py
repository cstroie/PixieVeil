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
from typing import Dict, Any, Optional

from aiohttp import web

from pixieveil.config import Settings
from pixieveil.storage.storage_manager import StorageManager

logger = logging.getLogger(__name__)


class Dashboard:
    """
    Main dashboard class handling web server setup and route management.
    
    This class provides the web-based dashboard interface for the PixieVeil application.
    It manages the HTTP server, handles web routes, and provides real-time updates
    through periodic API calls.
    
    The dashboard provides:
    - Main dashboard page with system status and metrics
    - JSON API endpoint for fetching current statistics
    - Real-time updates via periodic JavaScript API calls
    
    Attributes:
        settings (Settings): Application configuration settings
        app (web.Application): aiohttp web application instance
        runner (web.AppRunner): Application runner for the web server
        site (web.TCPSite): TCP site for serving the web application
    """
    
    def __init__(self, settings: Settings, storage_manager: StorageManager) -> None:
        """
        Initialize the Dashboard with application settings and storage manager.
        
        Args:
            settings: Application configuration settings containing HTTP server
                      configuration (IP address, port, etc.)
            storage_manager: Storage manager instance for accessing system metrics
        """
        self.settings = settings
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.app['storage_manager'] = storage_manager

    async def start(self) -> None:
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

        # Setup routes
        self.app.add_routes([
            web.get("/", self.handle_index),
            web.get("/stats", self.handle_stats),
            web.get("/health", self.handle_health),
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

    async def handle_index(self, request: web.Request) -> web.Response:
        """
        Handle the main dashboard page.
        
        This method serves the main dashboard page which provides an overview
        of the system status and all metrics in a single page.
        
        The page includes:
        - System status display
        - Real-time metrics and statistics
        - Navigation links
        - JavaScript for periodic API calls to update data
        
        Args:
            request (web.Request): The incoming HTTP request
            
        Returns:
            web.Response: HTML response containing the dashboard page
        """
        # Read the template file
        template_path = Path(__file__).parent / "templates" / "index.html"
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
            return web.Response(text=html_content, content_type="text/html")
        except FileNotFoundError:
            logger.error(f"Template file not found: {template_path}")
            return web.Response(text="Dashboard template not found", status=500)

    async def handle_stats(self, request: web.Request) -> web.Response:
        """
        Handle the JSON API endpoint for statistics.
        
        This method provides current system statistics in JSON format
        for the dashboard to consume and display.
        
        The API returns:
        - Server status
        - Image processing metrics
        - Study completion statistics
        - Performance metrics
        
        Args:
            request (web.Request): The incoming HTTP request
            
        Returns:
            web.Response: JSON response containing current statistics
        """
        storage_manager = request.app['storage_manager']
        
        # Get current metrics from storage manager
        studies_in_progress = len(storage_manager.study_states) if hasattr(storage_manager, 'study_states') else 0
        completed_studies = storage_manager.completed_count
        total_studies = completed_studies + studies_in_progress
        
        # Get all counters from storage manager
        counters = storage_manager.get_counters()
        
        stats = {
            "server_status": "running",
            "timestamp": asyncio.get_event_loop().time(),
            "counters": counters,
            "studies": {
                "completed": completed_studies,
                "in_progress": studies_in_progress,
                "total": total_studies
            }
        }
        
        return web.json_response(stats)

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        Simple health‑check endpoint.

        Returns a JSON payload indicating that the service is alive.
        """
        return web.json_response({"status": "ok"})
