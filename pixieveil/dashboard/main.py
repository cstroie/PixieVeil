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

from pixieveil.config import Settings
from pixieveil.dashboard.sse import image_counter, init_image_counter

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
        self.app['storage_manager'] = storage_manager
        # Initialize the image counter when dashboard is created
        init_image_counter()

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

        # Setup routes
        self.app.add_routes([
            web.get("/", self.handle_index),
            web.get("/stats", self.handle_stats),
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
            "image_count": image_counter.get_count(),
            "completed_studies": completed_studies,
            "studies_in_progress": studies_in_progress,
            "total_studies": total_studies,
            "average_processing_time": counters.get('average_processing_time', 0),
            
            # Reception metrics
            "received_studies": counters.get('received_studies', 0),
            "received_images": counters.get('received_images', 0),
            "received_bytes": counters.get('received_bytes', 0),
            
            # Processing metrics
            "processed_images": counters.get('processed_images', 0),
            "processed_studies": counters.get('processed_studies', 0),
            "anonymized_images": counters.get('anonymized_images', 0),
            "validation_errors": counters.get('validation_errors', 0),
            "anonymization_errors": counters.get('anonymization_errors', 0),
            "processing_errors": counters.get('processing_errors', 0),
            
            # Storage metrics
            "stored_studies": counters.get('stored_studies', 0),
            "stored_series": counters.get('stored_series', 0),
            "stored_images": counters.get('stored_images', 0),
            
            # Archive metrics
            "archived_studies": counters.get('archived_studies', 0),
            "archived_images": counters.get('archived_images', 0),
            "archive_errors": counters.get('archive_errors', 0),
            
            # Export metrics
            "exported_studies": counters.get('exported_studies', 0),
            "exported_images": counters.get('exported_images', 0),
            "export_errors": counters.get('export_errors', 0),
            
            # Remote storage metrics
            "uploaded_studies": counters.get('uploaded_studies', 0),
            "uploaded_images": counters.get('uploaded_images', 0),
            "upload_errors": counters.get('upload_errors', 0),
            "upload_bytes": counters.get('upload_bytes', 0),
            
            # Performance metrics
            "processing_time_total": counters.get('processing_time_total', 0),
            "processing_time_count": counters.get('processing_time_count', 0),
            
            # Cleanup metrics
            "cleaned_studies": counters.get('cleaned_studies', 0),
            "cleaned_images": counters.get('cleaned_images', 0),
            
            # Error metrics
            "total_errors": counters.get('total_errors', 0),
            "reconnection_attempts": counters.get('reconnection_attempts', 0),
            "timeout_errors": counters.get('timeout_errors', 0),
            
            "timestamp": asyncio.get_event_loop().time()
        }
        
        return web.json_response(stats)
