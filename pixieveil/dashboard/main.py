import asyncio
import logging
from pathlib import Path
from typing import Dict, Any

from aiohttp import web

from pixieveil.config import Settings
from pixieveil.dashboard.sse import ServerSentEvents

logger = logging.getLogger(__name__)

class Dashboard:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.app = web.Application()
        self.runner = None
        self.site = None
        self.sse = ServerSentEvents()

    async def start(self):
        logger.info("Starting dashboard")

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
        logger.info("Stopping dashboard")
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("Dashboard stopped")

    async def handle_index(self, request: web.Request) -> web.Response:
        """
        Handle the main dashboard page.
        """
        html = """
        <html>
            <head>
                <title>PixieVeil Dashboard</title>
                <script>
                    const eventSource = new EventSource("/events");
                    eventSource.onmessage = function(event) {
                        const data = JSON.parse(event.data);
                        document.getElementById("status").innerText = data.status;
                    };
                </script>
            </head>
            <body>
                <h1>PixieVeil Dashboard</h1>
                <p>Status: <span id="status">Loading...</span></p>
                <a href="/metrics">View Metrics</a>
                <a href="/status">View Status</a>
            </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def handle_metrics(self, request: web.Request) -> web.Response:
        """
        Handle the metrics page.
        """
        metrics = {
            "studies_processed": 0,
            "images_processed": 0,
            "average_processing_time": 0
        }

        html = f"""
        <html>
            <head>
                <title>PixieVeil Metrics</title>
            </head>
            <body>
                <h1>PixieVeil Metrics</h1>
                <ul>
                    <li>Studies Processed: {metrics['studies_processed']}</li>
                    <li>Images Processed: {metrics['images_processed']}</li>
                    <li>Average Processing Time: {metrics['average_processing_time']}ms</li>
                </ul>
                <a href="/">Back to Dashboard</a>
            </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def handle_status(self, request: web.Request) -> web.Response:
        """
        Handle the status page.
        """
        status = {
            "server_status": "running",
            "studies_in_progress": 0,
            "total_studies": 0
        }

        html = f"""
        <html>
            <head>
                <title>PixieVeil Status</title>
            </head>
            <body>
                <h1>PixieVeil Status</h1>
                <ul>
                    <li>Server Status: {status['server_status']}</li>
                    <li>Studies in Progress: {status['studies_in_progress']}</li>
                    <li>Total Studies: {status['total_studies']}</li>
                </ul>
                <a href="/">Back to Dashboard</a>
            </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
