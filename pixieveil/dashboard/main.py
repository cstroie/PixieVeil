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
from pixieveil.dashboard.sse import image_counter

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
        html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>PixieVeil Dashboard</title>
            <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css">
            <style>
                .metric-card {
                    background: white;
                    border-radius: 8px;
                    padding: 1rem;
                    margin: 0.5rem 0;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                }
                .metric-value {
                    font-size: 2rem;
                    font-weight: bold;
                    color: #2563eb;
                }
                .metric-label {
                    color: #6b7280;
                    font-size: 0.875rem;
                }
                .status-indicator {
                    display: inline-block;
                    width: 12px;
                    height: 12px;
                    border-radius: 50%;
                    margin-right: 0.5rem;
                }
                .status-running {
                    background-color: #10b981;
                }
                .status-stopped {
                    background-color: #ef4444;
                }
                .last-updated {
                    font-size: 0.75rem;
                    color: #9ca3af;
                    text-align: right;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <header>
                    <h1>PixieVeil Dashboard</h1>
                    <p>Real-time DICOM Anonymization Server</p>
                </header>
                
                <section>
                    <h2>System Status</h2>
                    <div class="metric-card">
                        <p><strong>Status:</strong> <span id="status"><span class="status-indicator status-stopped"></span>Loading...</span></p>
                        <p class="last-updated>Last updated: <span id="last-updated">Never</span></p>
                    </div>
                </section>
                
                <section>
                    <h2>Processing Metrics</h2>
                    <div class="metric-card">
                        <p><strong>Images Processed:</strong> <span id="image-count" class="metric-value">0</span></p>
                        <p class="metric-label">Total DICOM images processed</p>
                    </div>
                    
                    <div class="metric-card">
                        <p><strong>Studies Completed:</strong> <span id="completed-studies" class="metric-value">0</span></p>
                        <p class="metric-label">DICOM studies fully processed</p>
                    </div>
                    
                    <div class="metric-card">
                        <p><strong>Studies in Progress:</strong> <span id="studies-in-progress" class="metric-value">0</span></p>
                        <p class="metric-label">Currently being processed</p>
                    </div>
                </section>
                
                <section>
                    <h2>Performance Metrics</h2>
                    <div class="metric-card">
                        <p><strong>Total Studies:</strong> <span id="total-studies" class="metric-value">0</span></p>
                        <p class="metric-label">Studies processed + in progress</p>
                    </div>
                    
                    <div class="metric-card">
                        <p><strong>Average Processing Time:</strong> <span id="avg-processing-time" class="metric-value">0</span>ms</p>
                        <p class="metric-label">Average time per image</p>
                    </div>
                </section>
                
                <section>
                    <h2>Navigation</h2>
                    <nav>
                        <ul>
                            <li><button onclick="refreshData()" role="button">Refresh Data</button></li>
                            <li><button onclick="toggleAutoRefresh()" id="auto-refresh-btn" role="button">Enable Auto Refresh</button></li>
                        </ul>
                    </nav>
                </section>
            </div>

            <script>
                let autoRefreshInterval = null;
                let isAutoRefreshEnabled = false;

                async function fetchStats() {
                    try {
                        const response = await fetch('/stats');
                        if (!response.ok) {
                            throw new Error('Network response was not ok');
                        }
                        const data = await response.json();
                        updateDashboard(data);
                    } catch (error) {
                        console.error('Error fetching stats:', error);
                        document.getElementById('status').innerHTML = '<span class="status-indicator status-stopped"></span>Error';
                    }
                }

                function updateDashboard(data) {
                    // Update status
                    const statusElement = document.getElementById('status');
                    if (data.server_status === 'running') {
                        statusElement.innerHTML = '<span class="status-indicator status-running"></span>Running';
                    } else {
                        statusElement.innerHTML = '<span class="status-indicator status-stopped"></span>Stopped';
                    }

                    // Update metrics
                    document.getElementById('image-count').textContent = data.image_count || 0;
                    document.getElementById('completed-studies').textContent = data.completed_studies || 0;
                    document.getElementById('studies-in-progress').textContent = data.studies_in_progress || 0;
                    document.getElementById('total-studies').textContent = data.total_studies || 0;
                    document.getElementById('avg-processing-time').textContent = data.average_processing_time || 0;

                    // Update timestamp
                    const now = new Date();
                    document.getElementById('last-updated').textContent = now.toLocaleTimeString();
                }

                function refreshData() {
                    fetchStats();
                }

                function toggleAutoRefresh() {
                    const btn = document.getElementById('auto-refresh-btn');
                    
                    if (isAutoRefreshEnabled) {
                        // Disable auto refresh
                        clearInterval(autoRefreshInterval);
                        autoRefreshInterval = null;
                        isAutoRefreshEnabled = false;
                        btn.textContent = 'Enable Auto Refresh';
                        btn.classList.remove('secondary');
                    } else {
                        // Enable auto refresh
                        autoRefreshInterval = setInterval(fetchStats, 5000); // Refresh every 5 seconds
                        isAutoRefreshEnabled = true;
                        btn.textContent = 'Disable Auto Refresh';
                        btn.classList.add('secondary');
                    }
                }

                // Initial load
                document.addEventListener('DOMContentLoaded', function() {
                    fetchStats();
                });
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

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
        
        # Calculate average processing time (placeholder - implement actual calculation if needed)
        average_processing_time = 0
        
        stats = {
            "server_status": "running",
            "image_count": image_counter.get_count(),
            "completed_studies": completed_studies,
            "studies_in_progress": studies_in_progress,
            "total_studies": total_studies,
            "average_processing_time": average_processing_time,
            "timestamp": asyncio.get_event_loop().time()
        }
        
        return web.json_response(stats)
