"""
Dashboard Web Interface Module

This module provides the web-based dashboard interface for the PixieVeil application.
It serves as the main user interface for monitoring system status, viewing metrics,
 and managing DICOM study processing.

Classes:
    Dashboard: Main dashboard class handling web server setup and route management
"""

import asyncio
import functools
import importlib.metadata
import json
import logging
import platform
import time
from pathlib import Path
from typing import Any, Optional

from urllib.parse import quote, urlparse

from aiohttp import web

import pixieveil
from pixieveil.auth import user_store as user_store_mod
from pixieveil.auth.sessions import COOKIE_NAME, SessionSigner, load_or_create_secret
from pixieveil.auth.user_store import User, UserStore, UserStoreError
from pixieveil.config import Settings
from pixieveil.dashboard import options
from pixieveil.dicom_server.server import SUPPORTED_STORAGE_SOP_CLASSES
from pixieveil.processing import exam_merge
from pixieveil.storage.storage_manager import StorageManager

logger = logging.getLogger(__name__)

# Reachable without logging in. Everything else needs a session or token.
_PUBLIC_PATHS = frozenset({"/login", "/health", "/static/theme.css", "/static/common.js"})
# Sent by common.js on every request; a cookie-authenticated mutation
# without it is refused (a cross-site form post can't set custom headers).
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "PixieVeil"
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_S = 300.0

_VALID_ACTIONS = {
    "send_dicom", "reextract", "upload_http", "upload_rhythm", "mark_exported",
}

_DASHBOARD_DIR = Path(__file__).parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_STATIC_DIR = _DASHBOARD_DIR / "static"

# Distributions whose installed version /api/about reports. The second group
# only exists when the [deface] extra is installed.
_CORE_PACKAGES = ("pydicom", "pynetdicom", "aiohttp", "pydantic", "PyYAML")
_DEFACE_PACKAGES = ("torch", "nnunetv2", "SimpleITK", "nibabel", "numpy", "gdown")


@functools.lru_cache(maxsize=1)
def _package_versions() -> dict[str, Optional[str]]:
    """Installed versions (None if absent). Cached: they can't change while running."""
    versions: dict[str, Optional[str]] = {}
    for name in _CORE_PACKAGES + _DEFACE_PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


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
        http_cfg = settings.http_server
        users_file = Path(http_cfg.get("users_file") or user_store_mod.DEFAULT_PATH)
        self.user_store = UserStore(users_file)
        secret_file = Path(http_cfg.get("session_secret_file")
                           or users_file.parent / ".session_secret")
        self.sessions = SessionSigner(
            load_or_create_secret(secret_file),
            idle_s=float(http_cfg.get("session_idle_hours", 12)) * 3600,
            max_s=float(http_cfg.get("session_max_days", 7)) * 86400,
        )
        self.secure_cookies = bool(http_cfg.get("secure_cookies", False))
        # (ip, username) -> (consecutive failures, locked-until epoch)
        self._login_failures: dict[tuple[str, str], tuple[int, float]] = {}
        if http_cfg.get("auth_token"):
            logger.warning(
                "http_server.auth_token is no longer used — issue per-user API "
                "tokens with `python -m pixieveil.auth token-add <user> <name>`"
            )
        if self.user_store.count() == 0:
            logger.error(
                f"No dashboard users in {users_file} — nobody can log in. Create one "
                "with `python -m pixieveil.auth add <name> --admin`"
            )
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
            web.get("/login", self.handle_login_page),
            web.post("/login", self.handle_login),
            web.post("/logout", self.handle_logout),
            web.get("/api/me", self.handle_me),
            web.post("/api/me/password", self.handle_me_password),
            web.get("/api/me/tokens", self.handle_my_tokens),
            web.post("/api/me/tokens", self.handle_my_token_add),
            web.delete("/api/me/tokens/{id}", self.handle_my_token_revoke),
            web.get("/users", self.handle_users_page),
            web.get("/api/users", self.handle_users_list),
            web.post("/api/users", self.handle_users_add),
            web.patch("/api/users/{u}", self.handle_users_update),
            web.delete("/api/users/{u}", self.handle_users_delete),
            web.post("/api/users/{u}/password", self.handle_users_password),
            web.post("/api/users/{u}/logout", self.handle_users_logout),
            web.get("/", self.handle_index),
            web.get("/stats", self.handle_stats),
            web.get("/health", self.handle_health),
            web.get("/api/events", self.handle_events),
            web.get("/studies", self.handle_studies_page),
            web.get("/about", self.handle_about_page),
            web.get("/api/about", self.handle_about),
            web.get("/api/options", self.handle_options),
            web.get("/api/studies", self.handle_list_studies),
            # Registered before /api/studies/{n} so "rhythm-manifest" isn't
            # parsed as a study number.
            web.get("/api/studies/rhythm-manifest", self.handle_rhythm_manifest),
            web.get("/api/studies/{n}", self.handle_get_study),
            web.get("/api/studies/{n}/rhythm-manifest", self.handle_rhythm_manifest_one),
            web.put("/api/studies/{n}/exam", self.handle_save_exam),
            web.post("/api/studies/rhythm-batch", self.handle_rhythm_batch),
            web.post("/api/studies/{n}/actions/{action}", self.handle_study_action),
            web.static("/static", _STATIC_DIR),
        ])

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------

    @staticmethod
    def _presented_token(request: web.Request) -> Optional[str]:
        header_auth = request.headers.get("Authorization", "")
        if header_auth.startswith("Bearer "):
            return header_auth[len("Bearer "):].strip()
        return request.headers.get("X-Auth-Token")

    @staticmethod
    def _wants_html(request: web.Request) -> bool:
        return request.method == "GET" and "text/html" in request.headers.get("Accept", "")

    def _resolve_user(self, request: web.Request) -> tuple[Optional[User], Optional[str]]:
        """(user, via) from an API token or the session cookie, else (None, None)."""
        token = self._presented_token(request)
        if token:
            user = self.user_store.verify_token(token)
            return (user, "token") if user else (None, None)
        sess = self.sessions.verify(request.cookies.get(COOKIE_NAME))
        if sess is None:
            return None, None
        user = self.user_store.get_active(sess.username, sess.version)
        if user is None:
            return None, None
        request["session"] = sess
        return user, "session"

    @staticmethod
    def _same_origin(request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        return urlparse(origin).netloc == request.host

    def _set_session_cookie(self, resp: web.StreamResponse, user: User,
                            issued_at: Optional[float] = None) -> None:
        resp.set_cookie(
            COOKIE_NAME, self.sessions.issue(user.username, user.session_version, issued_at),
            max_age=self.sessions.cookie_max_age(issued_at), path="/",
            httponly=True, samesite="Strict", secure=self.secure_cookies,
        )

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """Require a logged-in user (session cookie) or API token everywhere
        except _PUBLIC_PATHS.

        Unauthenticated: browser page loads redirect to /login?next=…; API,
        SSE and mutating requests get a 401 JSON body. Cookie-authenticated
        mutations must also carry the X-Requested-With header and, if the
        browser sent an Origin, a same-origin one (CSRF guard); API-token
        requests are exempt since a browser never attaches those by itself.
        """
        if request.path in _PUBLIC_PATHS:
            return await handler(request)

        user, via = self._resolve_user(request)
        if user is None:
            if self._wants_html(request):
                raise web.HTTPFound(f"/login?next={quote(request.path_qs, safe='')}")
            return web.json_response({"error": "unauthorized"}, status=401)

        if via == "session" and request.method not in ("GET", "HEAD", "OPTIONS"):
            if (request.headers.get(CSRF_HEADER) != CSRF_VALUE
                    or not self._same_origin(request)):
                return web.json_response({"error": "CSRF check failed"}, status=403)

        request["user"] = user
        request["auth_via"] = via
        resp = await handler(request)
        sess = request.get("session")
        if (sess is not None and self.sessions.needs_refresh(sess)
                and isinstance(resp, web.StreamResponse) and not resp.prepared
                and COOKIE_NAME not in resp.cookies):
            self._set_session_cookie(resp, user, sess.issued_at)
        return resp

    @staticmethod
    def _actor(request: web.Request) -> Optional[str]:
        user = request.get("user")
        return user.username if user is not None else None

    @staticmethod
    def _require_admin(request: web.Request) -> None:
        user = request.get("user")
        if user is None or not user.admin:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "admin only"}), content_type="application/json"
            )

    @staticmethod
    def _safe_next(raw: Optional[str]) -> str:
        """Only same-site relative redirects (no //host, no scheme)."""
        if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
            return "/"
        return raw

    def _login_locked(self, key: tuple[str, str]) -> float:
        """Seconds left on a lockout for key (0 when not locked)."""
        _fails, until = self._login_failures.get(key, (0, 0.0))
        return max(0.0, until - time.time())

    def _login_failed(self, key: tuple[str, str]) -> None:
        fails, _until = self._login_failures.get(key, (0, 0.0))
        fails += 1
        until = time.time() + LOGIN_LOCKOUT_S if fails >= LOGIN_MAX_FAILURES else 0.0
        self._login_failures[key] = (0 if until else fails, until)
        if until:
            logger.warning(f"Login locked for {LOGIN_LOCKOUT_S:.0f}s: user={key[1]!r} ip={key[0]}")
        # Bound memory against a flood of random usernames.
        if len(self._login_failures) > 10000:
            now = time.time()
            self._login_failures = {k: v for k, v in self._login_failures.items()
                                    if v[1] > now or v[0] > 0}

    async def handle_login_page(self, request: web.Request) -> web.Response:
        if self._resolve_user(request)[0] is not None:
            raise web.HTTPFound(self._safe_next(request.query.get("next")))
        return await self._serve_template("login.html")

    async def handle_login(self, request: web.Request) -> web.Response:
        """POST {"username", "password", "next"?} → sets the session cookie."""
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE or not self._same_origin(request):
            return web.json_response({"error": "CSRF check failed"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid JSON body"}, status=400)
        username = str(body.get("username") or "").strip().lower()
        password = str(body.get("password") or "")
        if self.user_store.count() == 0:
            return web.json_response({
                "error": "No users are configured. On the server run: "
                         "python -m pixieveil.auth add <name> --admin",
            }, status=503)
        key = (request.remote or "?", username)
        left = self._login_locked(key)
        if left:
            return web.json_response(
                {"error": f"Too many failed attempts — try again in {int(left // 60) + 1} min"},
                status=429,
            )
        user = await asyncio.to_thread(self.user_store.authenticate, username, password)
        if user is None:
            self._login_failed(key)
            logger.warning(f"Failed login: user={username!r} ip={request.remote}")
            return web.json_response({"error": "Invalid username or password"}, status=401)
        self._login_failures.pop(key, None)
        logger.info(f"Login: user={user.username} ip={request.remote}")
        resp = web.json_response({"ok": True, "next": self._safe_next(body.get("next"))})
        self._set_session_cookie(resp, user)
        return resp

    async def handle_logout(self, request: web.Request) -> web.Response:
        logger.info(f"Logout: user={self._actor(request)}")
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE_NAME, path="/")
        return resp

    async def handle_me(self, request: web.Request) -> web.Response:
        user: User = request["user"]
        return web.json_response({"username": user.username, "admin": user.admin,
                                  "via": request.get("auth_via")})

    async def handle_me_password(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        user: User = request["user"]
        try:
            await asyncio.to_thread(self.user_store.change_own_password, user.username,
                                    str(body.get("current") or ""), str(body.get("new") or ""))
        except UserStoreError as e:
            return web.json_response({"error": str(e)}, status=400)
        # Other sessions die (session_version bumped); keep this one alive.
        fresh = self.user_store.get(user.username)
        resp = web.json_response({"ok": True})
        if fresh is not None and request.get("auth_via") == "session":
            self._set_session_cookie(resp, fresh)
        return resp

    async def handle_my_tokens(self, request: web.Request) -> web.Response:
        user = self.user_store.get(request["user"].username)
        return web.json_response({"tokens": user.public()["tokens"] if user else []})

    async def handle_my_token_add(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        try:
            token, meta = await asyncio.to_thread(
                self.user_store.issue_token, request["user"].username, str(body.get("name") or "")
            )
        except UserStoreError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "token": token, "meta": meta})

    async def handle_my_token_revoke(self, request: web.Request) -> web.Response:
        try:
            await asyncio.to_thread(self.user_store.revoke_token, request["user"].username,
                                    request.match_info["id"])
        except UserStoreError as e:
            return web.json_response({"error": str(e)}, status=404)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ admin: users
    @staticmethod
    async def _json_body(request: web.Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid JSON body"}),
                                     content_type="application/json")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text=json.dumps({"error": "body must be an object"}),
                                     content_type="application/json")
        return body

    async def handle_users_page(self, request: web.Request) -> web.Response:
        user = request.get("user")
        if user is None or not user.admin:
            raise web.HTTPFound("/")
        return await self._serve_template("users.html")

    async def handle_users_list(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        users = await asyncio.to_thread(self.user_store.list_users)
        return web.json_response({"users": [u.public() for u in users]})

    async def _admin_op(self, request: web.Request, what: str, fn, *args) -> web.Response:
        self._require_admin(request)
        try:
            await asyncio.to_thread(fn, *args)
        except UserStoreError as e:
            status = 404 if str(e).startswith("no such user") else 400
            return web.json_response({"error": str(e)}, status=status)
        logger.info(f"Admin: user={self._actor(request)} {what}")
        return web.json_response({"ok": True})

    async def handle_users_add(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        body = await self._json_body(request)
        return await self._admin_op(
            request, f"added user {body.get('username')!r}", self.user_store.add,
            str(body.get("username") or ""), str(body.get("password") or ""),
            bool(body.get("admin", False)),
        )

    async def handle_users_update(self, request: web.Request) -> web.Response:
        """PATCH {"admin"?: bool, "disabled"?: bool}."""
        self._require_admin(request)
        body = await self._json_body(request)
        name = request.match_info["u"]
        if name.lower() == request["user"].username and (
                body.get("disabled") is True or body.get("admin") is False):
            return web.json_response(
                {"error": "you cannot disable yourself or remove your own admin"}, status=400)
        for key, fn in (("admin", self.user_store.set_admin),
                        ("disabled", self.user_store.set_disabled)):
            if key in body:
                resp = await self._admin_op(request, f"set {key}={bool(body[key])} on {name!r}",
                                            fn, name, bool(body[key]))
                if resp.status != 200:
                    return resp
        return web.json_response({"ok": True})

    async def handle_users_delete(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        name = request.match_info["u"]
        if name.lower() == request["user"].username:
            return web.json_response({"error": "you cannot delete yourself"}, status=400)
        return await self._admin_op(request, f"deleted user {name!r}",
                                    self.user_store.delete, name)

    async def handle_users_password(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        body = await self._json_body(request)
        name = request.match_info["u"]
        return await self._admin_op(request, f"reset password of {name!r}",
                                    self.user_store.set_password, name,
                                    str(body.get("password") or ""))

    async def handle_users_logout(self, request: web.Request) -> web.Response:
        name = request.match_info["u"]
        return await self._admin_op(request, f"ended sessions of {name!r}",
                                    self.user_store.revoke_sessions, name)

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
        # Wake every open /api/events stream so it returns; otherwise the
        # site/runner shutdown below waits on those long-lived responses.
        self.app['storage_manager'].events.close_all()
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

    SSE_PING_S = 15.0

    async def handle_events(self, request: web.Request) -> web.StreamResponse:
        """Server-Sent Events stream of thin dashboard notifications.

        Frames: ``hello`` (on connect), ``study``/``exam``/``progress``/
        ``counters``. The page re-fetches via the regular endpoints; events
        carry no data beyond study numbers, statuses and progress counters.
        A ``: ping`` comment every SSE_PING_S keeps proxies from idling out.
        """
        bus = request.app['storage_manager'].events
        last_raw = request.headers.get("Last-Event-ID") or request.query.get("last_id")
        try:
            last_id: Optional[int] = int(last_raw) if last_raw else None
        except ValueError:
            last_id = None

        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        sub, resync = bus.subscribe(last_id)

        async def send(event_id: Optional[int], type_: str, data: dict) -> None:
            frame = ""
            if event_id is not None:
                frame += f"id: {event_id}\n"
            frame += f"event: {type_}\ndata: {json.dumps(data)}\n\n"
            await resp.write(frame.encode())

        try:
            await send(bus.last_id or None, "hello", {
                "server_started": bus.started_at, "resync": resync,
            })
            while not sub.dropped:
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=self.SSE_PING_S)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                if ev.type == "_close":
                    break
                await send(ev.id, ev.type, ev.data)
        except ConnectionResetError:
            pass  # client went away mid-write
        finally:
            bus.unsubscribe(sub)
        return resp

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
                "rhythm": sm.rhythm_storage.enabled,
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

    async def handle_about_page(self, request: web.Request) -> web.Response:
        """Serve the /about page (software info, this instance, credits)."""
        return await self._serve_template("about.html")

    async def handle_about(self, request: web.Request) -> web.Response:
        """
        Software and instance information for the /about page.

        Never includes secrets: auth tokens are reported only as on/off, and
        the HTTP export target only by host.
        """
        sm: StorageManager = request.app['storage_manager']
        cfg = self.settings
        versions = await asyncio.to_thread(_package_versions)

        model_present = None
        if sm.defacer.model_dir is not None:
            model_present = (sm.defacer.model_dir / "Dataset001_DEFACE").exists()

        dicom_export = None
        if sm.dicom_storage.enabled:
            dicom_export = {"ae_title": sm.dicom_storage.ae_title,
                            "host": sm.dicom_storage.host, "port": sm.dicom_storage.port}
        http_export = None
        if sm.remote_storage.enabled:
            http_export = {"host": urlparse(sm.remote_storage.base_url).netloc or None}

        return web.json_response({
            "software": {
                "name": "PixieVeil",
                "version": pixieveil.__version__,
                "python": platform.python_version(),
                "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            },
            "instance": {
                "uptime_s": int(time.time() - self._started_at),
                "dicom": {"ae_title": cfg.dicom_server.get("ae_title"),
                          "port": cfg.dicom_server.get("port")},
                "http_port": cfg.http_server.get("port"),
                "auth": {"mode": "users", "user_count": self.user_store.count()},
                "anonymization_profile": cfg.anonymization.get("profile"),
                "defacing": {"enabled": sm.defacer.enabled,
                             "device": sm.defacer.device if sm.defacer.enabled else None,
                             "model_present": model_present if sm.defacer.enabled else None},
                "export": {"dicom": dicom_export, "http": http_export},
                "retain_after_export_days": cfg.storage.get("retain_after_export_days"),
                "max_storage_gb": cfg.storage.get("max_storage_gb"),
                "completion_timeout_s": cfg.study.get("completion_timeout"),
                "sop_classes": [c.name for c in SUPPORTED_STORAGE_SOP_CLASSES],
            },
            "packages": {
                "core": {n: versions[n] for n in _CORE_PACKAGES},
                "deface": {n: versions[n] for n in _DEFACE_PACKAGES},
            },
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
                "rhythm": storage_manager.rhythm_storage.enabled,
                # so the page can explain a disabled RHYTHM button ("requires
                # the 'gdpr' profile") rather than just greying it out.
                "anonymization_profile": storage_manager.settings.anonymization.get(
                    "profile", "research"
                ),
                # local protocol keys from config/rhythm_protocols.yaml, for
                # the exam panel's "RHYTHM protocol" dropdown
                "rhythm_protocols": [
                    {"id": e.id, "name": e.name, "description": e.description,
                     "uuid_pending": e.uuid_pending}
                    for e in storage_manager.rhythm_storage.protocol_map.entries
                ],
            },
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

        result = await storage_manager.save_exam(n, fields, actor=self._actor(request))
        await asyncio.to_thread(
            storage_manager.record_audit, n, self._actor(request), "save_exam",
            bool(result.get("ok")), ",".join(sorted(fields)) or None,
        )
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

        if action == "mark_exported":
            # Out-of-band export: partner tooling shipped export_path/<n>.zip
            # to RHYTHM itself and calls this so PixieVeil archives the study
            # and starts its retention clock. Optional JSON body:
            # {"via": "rhythm"|"http"|"dicom", "sha256": "<hex>"}.
            via, sha256 = "rhythm", None
            if request.can_read_body:
                try:
                    body = await request.json()
                except Exception:
                    return web.json_response({"error": "invalid JSON body"}, status=400)
                if isinstance(body, dict):
                    via = body.get("via", "rhythm")
                    sha256 = body.get("sha256")
            result = await storage_manager.mark_exported(n, via=via, sha256=sha256)
        else:
            method = {
                "send_dicom": storage_manager.manual_send_dicom,
                "reextract": storage_manager.manual_reextract,
                "upload_http": storage_manager.manual_upload_http,
                "upload_rhythm": storage_manager.manual_upload_rhythm,
            }[action]
            result = await method(n)
        await asyncio.to_thread(
            storage_manager.record_audit, n, self._actor(request), action,
            bool(result.get("ok")), None if result.get("ok") else result.get("message"),
        )
        if result.get("ok"):
            return web.json_response(result)
        message = result.get("message", "")
        status = 409 if "in progress" in message else 400
        return web.json_response(result, status=status)

    async def _rhythm_manifest_response(
        self, storage_manager: "StorageManager", numbers: list, request: web.Request
    ) -> web.Response:
        # For a single already-exported study, serve the manifest written to
        # export_path/<NNNN>.json verbatim (real sha256/size_bytes, the batch
        # it actually went up under) rather than minting a fresh preview with
        # a new batch id and no checksums.
        manifest = None
        if len(numbers) == 1:
            manifest = await asyncio.to_thread(
                storage_manager.load_export_manifest_sync, numbers[0]
            )
        if manifest is None:
            manifest = await asyncio.to_thread(
                storage_manager.build_rhythm_manifest_preview_sync, numbers
            )
        if request.query.get("download"):
            # Pristine v2 manifest, as a file — strip the preview-only
            # `blockers` key so it can go straight into RHYTHM's own
            # validate-manifest endpoint / json-validator page.
            manifest = {k: v for k, v in manifest.items() if k != "blockers"}
            return web.Response(
                text=json.dumps(manifest, indent=2),
                content_type="application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="rhythm_manifest.json"'
                },
            )
        return web.json_response(manifest)

    async def handle_rhythm_manifest(self, request: web.Request) -> web.Response:
        """Preview/download the RHYTHM v2 batch manifest for the studies in
        the ``?studies=1,2,3`` query (add ``&download=1`` for a file). Builds
        the manifest without uploading; the JSON view adds a top-level
        `blockers` key naming anything that would stop a real upload (the
        download omits it)."""
        storage_manager: StorageManager = request.app['storage_manager']
        raw = request.query.get("studies", "")
        try:
            numbers = [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            return web.json_response(
                {"error": "studies must be a comma-separated list of integers"},
                status=400,
            )
        if not numbers:
            return web.json_response({"error": "no study numbers given"}, status=400)
        return await self._rhythm_manifest_response(storage_manager, numbers, request)

    async def handle_rhythm_manifest_one(self, request: web.Request) -> web.Response:
        """Same as handle_rhythm_manifest for a single study (a 1-item batch).

        If the study has already been exported, this returns the manifest
        saved at export_path/<NNNN>.json verbatim.
        """
        n = self._coerce_study_number(request)
        storage_manager: StorageManager = request.app['storage_manager']
        return await self._rhythm_manifest_response(storage_manager, [n], request)

    async def handle_rhythm_batch(self, request: web.Request) -> web.Response:
        """Upload several studies to RHYTHM as one batch (one dashboard
        action → one server-assigned batch id)."""
        storage_manager: StorageManager = request.app['storage_manager']
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        numbers = body.get("study_numbers")
        if not isinstance(numbers, list) or not all(
            isinstance(x, int) for x in numbers
        ):
            return web.json_response(
                {"error": 'body must be {"study_numbers": [int, ...]}'}, status=400
            )

        result = await storage_manager.manual_upload_rhythm_batch(numbers)
        await asyncio.to_thread(self._audit_batch, storage_manager, numbers, result,
                                self._actor(request))
        return web.json_response(result)

    @staticmethod
    def _audit_batch(sm: StorageManager, numbers: list, result: dict,
                     actor: Optional[str]) -> None:
        per_study = result.get("results") or {}
        for n in numbers:
            r = per_study.get(str(n)) or result
            ok = bool(r.get("ok"))
            sm.record_audit(n, actor, "upload_rhythm", ok,
                            f"batch {result.get('batch')}" if ok else r.get("message"))
