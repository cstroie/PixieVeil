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
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from pixieveil.config import Settings
from pixieveil.processing import exam_merge
from pixieveil.storage.storage_manager import StorageManager

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"send_dicom", "reextract", "upload_http"}


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
        # Registered here (not in start()) because aiohttp freezes the
        # application's middleware list once runner.setup() runs — adding it
        # later would silently leave every route unauthenticated.
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.app['storage_manager'] = storage_manager

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """
        Guard non-GET requests with an optional shared token.

        When ``http_server.auth_token`` is unset (default), this is a no-op
        and behavior is unchanged from before this middleware existed. When
        set, any non-GET request (the mutating /api/studies/* endpoints)
        must present it as either ``Authorization: Bearer <token>`` or
        ``X-Auth-Token: <token>``.
        """
        token = self.settings.http_server.get("auth_token")
        if token and request.method != "GET":
            header_auth = request.headers.get("Authorization", "")
            presented = None
            if header_auth.startswith("Bearer "):
                presented = header_auth[len("Bearer "):]
            if presented is None:
                presented = request.headers.get("X-Auth-Token")
            if presented != token:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

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
            web.get("/studies", self.handle_studies_page),
            web.get("/api/studies", self.handle_list_studies),
            web.get("/api/studies/{n}", self.handle_get_study),
            web.put("/api/studies/{n}/exam", self.handle_save_exam),
            web.post("/api/studies/{n}/actions/{action}", self.handle_study_action),
        ])

        # Create runner and site
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(
            self.runner,
            self.settings.http_server["ip"],
            self.settings.http_server["port"],
            reuse_address=True,
        )

        # Start the site
        try:
            await self.site.start()
        except OSError as e:
            host = self.settings.http_server["ip"]
            port = self.settings.http_server["port"]
            logger.error(f"Dashboard cannot bind to {host}:{port} — address already in use: {e}")
            raise

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
            try:
                await asyncio.wait_for(self.site.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Dashboard site stop timed out after 5 seconds")
            except Exception as e:
                logger.error(f"Error stopping dashboard site: {e}")
            finally:
                self.site = None

        if self.runner:
            try:
                await asyncio.wait_for(self.runner.cleanup(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Dashboard runner cleanup timed out after 5 seconds")
            except Exception as e:
                logger.error(f"Error during dashboard runner cleanup: {e}")
            finally:
                self.runner = None

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
            html_content = await asyncio.to_thread(template_path.read_text, encoding='utf-8')
            return web.Response(text=html_content, content_type="text/html")
        except FileNotFoundError:
            logger.error(f"Template file not found: {template_path}")
            return web.Response(text="Dashboard template not found", status=500)

    async def handle_stats(self, request: web.Request) -> web.Response:
        """
        Handle the JSON API endpoint for statistics.
        
        This method provides current system statistics in JSON format
        for the dashboard to consume and display. The response structure
        mirrors the dashboard layout with sections and metrics.
        
        The API returns:
        - server_status: "running" or "stopped"
        - timestamp: current time
        - sections: list of sections, each with title and metrics array
        
        Each metric has:
        - label: display label
        - value: numeric value (or formatted string for complex metrics)
        - suffix: optional unit suffix (e.g., "MB", "ms")
        
        Args:
            request (web.Request): The incoming HTTP request
            
        Returns:
            web.Response: JSON response containing current statistics
        """
        storage_manager = request.app['storage_manager']
        
        studies_in_progress = storage_manager.study_manager.get_active_study_count()
        completed_studies = storage_manager.study_manager.get_completed_study_count()
        total_studies = completed_studies + studies_in_progress
        
        def bytes_to_mb(bytes_val):
            return int(round(bytes_val / (1024 * 1024)))
        
        # Get processing errors dict for nested access
        processing_errors = storage_manager.get_counter('processing', 'errors')
        if not isinstance(processing_errors, dict):
            processing_errors = {}
        
        # Build sections structure using get_counter method
        sections = [
            {
                "title": "Processed",
                "metrics": [
                    {"label": "Studies", "value": total_studies, "suffix": "total",
                     "sub_value": studies_in_progress, "sub_suffix": "in progress"},
                    {"label": "Completed", "value": completed_studies, "suffix": "studies"},
                    {"label": "Images", "value": storage_manager.get_counter('processing', 'images'), "suffix": "processed",
                     "sub_value": storage_manager.get_counter('processing', 'filtered_images'), "sub_suffix": "filtered"},
                    {"label": "Anonymized", "value": storage_manager.get_counter('processing', 'anonymized_images'), "suffix": "images"},
                ]
            },
            {
                "title": "Reception",
                "metrics": [
                    {"label": "Received", "value": storage_manager.get_counter('reception', 'studies'), "suffix": "studies",
                     "sub_value": storage_manager.get_counter('reception', 'images'), "sub_suffix": "images"},
                    {"label": "Data", "value": bytes_to_mb(storage_manager.get_counter('reception', 'bytes')), "suffix": "MB"},
                ]
            },
            {
                "title": "Storage",
                "metrics": [
                    {"label": "Stored", "value": storage_manager.get_counter('storage', 'studies'), "suffix": "studies",
                     "sub_value": storage_manager.get_counter('storage', 'images'), "sub_suffix": "images"},
                    {"label": "Series", "value": storage_manager.get_counter('storage', 'series')},
                ]
            },
            {
                "title": "Export",
                "metrics": [
                    {"label": "Archived", "value": storage_manager.get_counter('archive', 'studies'), "suffix": "studies",
                     "sub_value": storage_manager.get_counter('archive', 'images'), "sub_suffix": "images"},
                    {"label": "DICOM Export", "value": storage_manager.get_counter('export', 'dicom_studies'), "suffix": "studies",
                     "sub_value": storage_manager.get_counter('export', 'dicom_images'), "sub_suffix": "images"},
                    {"label": "HTTP Export", "value": storage_manager.get_counter('export', 'http_studies'), "suffix": "studies",
                     "sub_value": bytes_to_mb(storage_manager.get_counter('export', 'http_bytes')), "sub_suffix": "MB"},
                    {"label": "Cleaned Up", "value": storage_manager.get_counter('cleanup', 'studies'), "suffix": "studies",
                     "sub_value": storage_manager.get_counter('cleanup', 'images'), "sub_suffix": "images"},
                ]
            },
            {
                "title": "Performance",
                "metrics": [
                    {"label": "Average Time", "value": int(round(storage_manager.get_counter('performance', 'average_time', 0) * 1000)), "suffix": "ms"},
                    {"label": "Total Time", "value": int(round(storage_manager.get_counter('performance', 'total_time', 0) * 1000)), "suffix": "ms"},
                    {"label": "Images Anonymized", "value": storage_manager.get_counter('processing', 'anonymized_images')},
                ]
            },
            {
                "title": "Defacing",
                "metrics": [
                    {"label": "Studies Defaced", "value": storage_manager.get_counter('defacing', 'studies')},
                    {"label": "Series Defaced", "value": storage_manager.get_counter('defacing', 'series_defaced')},
                    {"label": "Series Skipped", "value": storage_manager.get_counter('defacing', 'series_skipped')},
                    {"label": "Errors", "value": storage_manager.get_counter('defacing', 'errors')},
                ]
            },
            {
                "title": "Errors",
                "class": "errors",
                "metrics": [
                    {"label": "Total", "value": storage_manager.get_counter('errors', 'total')},
                    {"label": "Validation", "value": processing_errors.get('validation', 0)},
                    {"label": "Anonymization", "value": processing_errors.get('anonymization', 0)},
                    {"label": "Processing", "value": processing_errors.get('processing', 0)},
                    {"label": "Export", "value": storage_manager.get_counter('export', 'errors')},
                ]
            }
        ]
        
        stats = {
            "server_status": "running",
            "timestamp": time.time(),
            "defacing_enabled": storage_manager.defacer.enabled,
            "pipeline_status": storage_manager.get_pipeline_status(),
            "sections": sections
        }
        
        return web.json_response(stats)

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        Simple health‑check endpoint.

        Returns a JSON payload indicating that the service is alive.
        """
        return web.json_response({"status": "ok"})

    # -----------------------------------------------------------------
    # /studies page + API
    # -----------------------------------------------------------------

    async def handle_studies_page(self, request: web.Request) -> web.Response:
        """Serve the /studies dashboard page (same pattern as handle_index)."""
        template_path = Path(__file__).parent / "templates" / "studies.html"
        try:
            html_content = await asyncio.to_thread(template_path.read_text, encoding='utf-8')
            return web.Response(text=html_content, content_type="text/html")
        except FileNotFoundError:
            logger.error(f"Template file not found: {template_path}")
            return web.Response(text="Studies template not found", status=500)

    @staticmethod
    def _coerce_study_number(request: web.Request) -> int:
        raw = request.match_info.get("n", "")
        try:
            return int(raw)
        except ValueError:
            raise web.HTTPBadRequest(text=f"invalid study number: {raw!r}")

    async def handle_list_studies(self, request: web.Request) -> web.Response:
        storage_manager: StorageManager = request.app['storage_manager']
        studies = await asyncio.to_thread(storage_manager.list_studies_summary_sync)
        return web.json_response({
            "studies": studies,
            "capabilities": {
                "dicom": storage_manager.dicom_storage.enabled,
                "http": storage_manager.remote_storage.enabled,
            },
            "auth_required": bool(self.settings.http_server.get("auth_token")),
        })

    async def handle_get_study(self, request: web.Request) -> web.Response:
        n = self._coerce_study_number(request)
        storage_manager: StorageManager = request.app['storage_manager']
        result = await asyncio.to_thread(storage_manager.get_study_detail_sync, n)
        if result is None:
            return web.json_response({"error": "study not found"}, status=404)
        return web.json_response(result)

    async def handle_save_exam(self, request: web.Request) -> web.Response:
        n = self._coerce_study_number(request)
        storage_manager: StorageManager = request.app['storage_manager']
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        fields = body.get("fields")
        if not isinstance(fields, dict):
            return web.json_response({"error": "body must be {\"fields\": {...}}"}, status=400)

        bad_keys = [k for k in fields if not exam_merge.is_editable_path(k)]
        if bad_keys:
            return web.json_response(
                {"error": "unknown/unwritable field(s)", "keys": bad_keys}, status=400
            )

        result = await storage_manager.save_exam(n, fields)
        if result.get("ok"):
            return web.json_response(result)
        message = result.get("message", "")
        status = 409 if "in progress" in message else 404
        return web.json_response(result, status=status)

    async def handle_study_action(self, request: web.Request) -> web.Response:
        n = self._coerce_study_number(request)
        action = request.match_info.get("action", "")
        if action not in _VALID_ACTIONS:
            return web.json_response({"error": f"unknown action {action!r}"}, status=400)

        storage_manager: StorageManager = request.app['storage_manager']
        method = {
            "send_dicom": storage_manager.manual_send_dicom,
            "reextract": storage_manager.manual_reextract,
            "upload_http": storage_manager.manual_upload_http,
        }[action]

        result = await method(n)
        if result.get("ok"):
            return web.json_response(result)
        message = result.get("message", "")
        status = 409 if "in progress" in message else 400
        return web.json_response(result, status=status)
