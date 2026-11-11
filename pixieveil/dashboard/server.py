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
from typing import Any, Optional

from aiohttp import web

from pixieveil.config import Settings
from pixieveil.dashboard import options
from pixieveil.processing import exam_merge
from pixieveil.storage.storage_manager import StorageManager

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"send_dicom", "reextract", "upload_http"}

_DASHBOARD_DIR = Path(__file__).parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_STATIC_DIR = _DASHBOARD_DIR / "static"


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
        self._started_at = time.time()
        # Routes are registered here rather than in start() so tests can
        # wrap self.app in an aiohttp TestServer without binding a port.
        self.app.add_routes([
            web.get("/", self.handle_index),
            web.get("/stats", self.handle_stats),
            web.get("/health", self.handle_health),
            web.get("/studies", self.handle_studies_page),
            web.get("/api/options", self.handle_options),
            web.get("/api/studies", self.handle_list_studies),
            web.get("/api/studies/{n}", self.handle_get_study),
            web.put("/api/studies/{n}/exam", self.handle_save_exam),
            web.post("/api/studies/{n}/actions/{action}", self.handle_study_action),
            web.static("/static", _STATIC_DIR),
        ])

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

        Routes are already registered in __init__; this creates the runner
        and TCP site and starts serving on the configured IP address and port.

        The method:
        - Creates the application runner and TCP site
        - Starts the web server
        - Logs the server address for user access

        Raises:
            Exception: If the web server fails to start
        """
        logger.info("Starting dashboard")

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

        host = self.settings.http_server['ip']
        port = self.settings.http_server['port']
        logger.info(f"Dashboard started on http://{host}:{port}")

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

    @staticmethod
    async def _serve_template(name: str) -> web.Response:
        """Serve one static HTML page from templates/ (no server-side templating)."""
        template_path = _TEMPLATES_DIR / name
        try:
            html_content = await asyncio.to_thread(template_path.read_text, encoding='utf-8')
        except FileNotFoundError:
            logger.error(f"Template file not found: {template_path}")
            return web.Response(text="Dashboard template not found", status=500)
        return web.Response(text=html_content, content_type="text/html")

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve the status page (pipeline, action callouts, KPIs, counters)."""
        return await self._serve_template("index.html")

    async def handle_stats(self, request: web.Request) -> web.Response:
        """
        JSON snapshot for the status page.

        - ``pipeline.active``: which stages have work in flight right now
        - ``pipeline.status_counts``: studies per sidecar status
        - ``kpis``: the handful of headline numbers
        - ``details``: every other counter, grouped, for the collapsible drawer
        """
        sm: StorageManager = request.app['storage_manager']
        status_counts = await asyncio.to_thread(sm.get_status_counts_sync)

        def c(category: str, sub: str) -> Any:
            return sm.get_counter(category, sub, 0)

        def mb(bytes_val: Any) -> float:
            return round((bytes_val or 0) / (1024 * 1024), 1)

        processing_errors = sm.get_counter('processing', 'errors')
        if not isinstance(processing_errors, dict):
            processing_errors = {}

        exported = c('export', 'dicom_studies') + c('export', 'http_studies')
        kpis = [
            {"key": "received", "label": "Images received", "value": c('reception', 'images'),
             "sub": f"{c('reception', 'studies')} studies · {mb(c('reception', 'bytes'))} MB"},
            {"key": "anonymized", "label": "Anonymized",
             "value": c('processing', 'anonymized_images'),
             "sub": f"{c('processing', 'filtered_images')} filtered out"},
            {"key": "defaced", "label": "Series defaced", "value": c('defacing', 'series_defaced'),
             "sub": f"{c('defacing', 'series_skipped')} skipped (non-head)"},
            {"key": "exported", "label": "Exports", "value": exported,
             "sub": f"{c('export', 'dicom_studies')} DICOM · {c('export', 'http_studies')} HTTP"},
            {"key": "errors", "label": "Errors", "value": c('errors', 'total'),
             "sub": "since start", "alert": c('errors', 'total') > 0},
        ]

        avg_ms = int(round((c('performance', 'average_time') or 0) * 1000))
        details = [
            {"title": "Reception", "rows": [
                ["Studies", c('reception', 'studies')],
                ["Images", c('reception', 'images')],
                ["Data", f"{mb(c('reception', 'bytes'))} MB"],
            ]},
            {"title": "Storage", "rows": [
                ["Studies", c('storage', 'studies')],
                ["Series", c('storage', 'series')],
                ["Images", c('storage', 'images')],
            ]},
            {"title": "Export", "rows": [
                ["DICOM studies", c('export', 'dicom_studies')],
                ["DICOM images", c('export', 'dicom_images')],
                ["HTTP studies", c('export', 'http_studies')],
                ["HTTP data", f"{mb(c('export', 'http_bytes'))} MB"],
                ["Cleaned up", f"{c('cleanup', 'studies')} studies"],
            ]},
            {"title": "Defacing", "rows": [
                ["Studies", c('defacing', 'studies')],
                ["Series defaced", c('defacing', 'series_defaced')],
                ["Series skipped", c('defacing', 'series_skipped')],
                ["Errors", c('defacing', 'errors')],
            ]},
            {"title": "Errors", "rows": [
                ["Validation", processing_errors.get('validation', 0)],
                ["Anonymization", processing_errors.get('anonymization', 0)],
                ["Processing", processing_errors.get('processing', 0)],
                ["Export", c('export', 'errors')],
            ]},
            {"title": "Performance", "rows": [
                ["Avg. per image", f"{avg_ms} ms"],
            ]},
        ]

        return web.json_response({
            "server_status": "running",
            "timestamp": time.time(),
            "uptime_s": int(time.time() - self._started_at),
            "defacing_enabled": sm.defacer.enabled,
            "capabilities": {
                "dicom": sm.dicom_storage.enabled,
                "http": sm.remote_storage.enabled,
            },
            "pipeline": {
                "active": sm.get_pipeline_status(),
                "status_counts": status_counts,
            },
            "kpis": kpis,
            "details": details,
        })

    async def handle_options(self, request: web.Request) -> web.Response:
        """Fixed vocabularies for the /studies review form."""
        sm: StorageManager = request.app['storage_manager']
        return web.json_response({
            "region_indications": options.REGION_INDICATIONS,
            "image_quality": options.IMAGE_QUALITY,
            "iv_contrast": options.IV_CONTRAST,
            "scanners": options.scanner_options(sm.exam_extractor.scanner_map),
            "required_fields": list(exam_merge.REQUIRED_PATHS),
        })

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
        """Serve the /studies review page."""
        return await self._serve_template("studies.html")

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
