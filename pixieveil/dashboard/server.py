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
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from urllib.parse import quote, urlparse

import yaml
from aiohttp import web

import pixieveil
from pixieveil.auth import user_store as user_store_mod
from pixieveil.auth.sessions import COOKIE_NAME, SessionSigner, load_or_create_secret
from pixieveil.auth.user_store import User, UserStore, UserStoreError
from pixieveil.config import Settings, settings_editor
from pixieveil.dashboard import options
from pixieveil.dashboard.setup import SetupRoutes, SetupState
from pixieveil.dicom_server.server import SUPPORTED_STORAGE_SOP_CLASSES
from pixieveil.processing import exam_merge, protocol_catalog, protocol_draft, rhythm_import
from pixieveil.storage import rhythm_storage as rhythm_storage_mod
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
RHYTHM_LOGIN_TTL_S = 300.0

_VALID_ACTIONS = {
    "send_dicom", "reextract", "upload_http", "upload_rhythm", "mark_exported",
}

_DASHBOARD_DIR = Path(__file__).parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_RHYTHM_CHOICES_FILE = _DASHBOARD_DIR / "rhythm_choices.json"
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
        # Registered here (not in start()) because aiohttp freezes the
        # application's middleware list once runner.setup() runs — adding it
        # later would silently leave every route unauthenticated.
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.app['storage_manager'] = storage_manager
        self._started_at = time.time()
        # /settings edits the file this process was started from — never the
        # shipped example it falls back to when settings.yaml is missing.
        src = settings.source_path
        self.settings_editor: Optional[settings_editor.SettingsEditor] = None
        if src is not None and src.name != "settings.example.yaml":
            self.settings_editor = settings_editor.SettingsEditor(
                Path(src), settings,
                catalog_site_code=lambda: storage_manager.protocol_catalog.site_code,
            )
        # Routes are registered here rather than in start() so tests can
        # wrap self.app in an aiohttp TestServer without binding a port.
        # RHYTHM sign-ins waiting for their 2FA code: id -> (admin, base_url,
        # cookie jar, expires). Never holds the password.
        self._rhythm_logins: dict[str, tuple[str, str, Any, float]] = {}
        # First-run wizard: its state lives next to the users file (config/).
        self.setup = SetupState(self, users_file.parent)
        self.app.add_routes(SetupRoutes(self, self.setup).routes())
        self.app.add_routes([
            web.get("/login", self.handle_login_page),
            web.post("/login", self.handle_login),
            web.post("/logout", self.handle_logout),
            web.get("/api/me", self.handle_me),
            web.post("/api/me/password", self.handle_me_password),
            web.get("/api/me/tokens", self.handle_my_tokens),
            web.post("/api/me/tokens", self.handle_my_token_add),
            web.delete("/api/me/tokens/{id}", self.handle_my_token_revoke),
            web.get("/protocols", self.handle_protocols_page),
            web.get("/api/protocols", self.handle_protocols_get),
            web.put("/api/protocols", self.handle_protocols_put),
            web.get("/api/protocols/unmatched", self.handle_protocols_unmatched),
            web.post("/api/protocols/rhythm-import", self.handle_protocols_rhythm_import),
            web.get("/settings", self.handle_settings_page),
            web.get("/api/settings", self.handle_settings_get),
            web.put("/api/settings", self.handle_settings_put),
            web.post("/api/settings/rhythm-test", self.handle_settings_rhythm_test),
            web.post("/api/settings/rhythm-login", self.handle_settings_rhythm_login),
            web.post("/api/settings/rhythm-login/verify", self.handle_settings_rhythm_verify),
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
            web.get("/api/studies/{n}/protocol-draft", self.handle_protocol_draft_get),
            web.post("/api/studies/{n}/protocol-draft", self.handle_protocol_draft_post),
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
        if request.path == "/setup" or request.path.startswith("/api/setup"):
            # The wizard routes gate themselves (setup code / admin) and 404
            # once setup is complete.
            return await handler(request)

        setup_required = self.setup.required
        if setup_required and self.setup.needs_admin:
            # Nobody can log in yet: everything leads to the wizard.
            if self._wants_html(request):
                raise web.HTTPFound("/setup")
            return web.json_response({"error": "setup required", "setup_required": True},
                                     status=401)

        user, via = self._resolve_user(request)
        if (setup_required and user is not None and user.admin and via == "session"
                and self._wants_html(request) and request.path in ("/", "/studies")):
            raise web.HTTPFound("/setup")
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
        if self.setup.required and self.setup.needs_admin:
            raise web.HTTPFound("/setup")
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
                "error": "No users yet: finish first-run setup at /setup, or on the "
                         "server run: python -m pixieveil.auth add <name> --admin",
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

    # ------------------------------------------------------------ protocols catalog
    async def handle_protocols_page(self, request: web.Request) -> web.Response:
        return await self._serve_template("protocols.html")

    async def handle_protocols_get(self, request: web.Request) -> web.Response:
        """The scanners & protocols catalog, its version (for PUT), per-protocol
        study counts, and whether this user may edit it."""
        sm: StorageManager = request.app['storage_manager']
        catalog = sm.protocol_catalog
        doc = await asyncio.to_thread(catalog.document)
        usage = await asyncio.to_thread(sm.catalog_usage_sync)
        return web.json_response({
            "catalog": doc,
            "version": catalog.version,
            "usage": usage,
            "editable": bool(request["user"].admin),
            "path": str(catalog.path),
        })

    async def handle_protocols_put(self, request: web.Request) -> web.Response:
        """Replace the catalog: ``{"catalog": {...}, "version": "<from GET>"}``.

        400 with ``errors`` on invalid content, 409 if someone else saved in
        between. On success every not-yet-exported study is re-matched.
        """
        self._require_admin(request)
        body = await self._json_body(request)
        doc = body.get("catalog")
        if not isinstance(doc, dict):
            return web.json_response({"error": 'body must be {"catalog": {...}, "version": …}'},
                                     status=400)
        sm: StorageManager = request.app['storage_manager']
        try:
            version = await asyncio.to_thread(
                sm.protocol_catalog.replace, doc, expected_version=body.get("version"))
        except protocol_catalog.CatalogConflictError as e:
            return web.json_response({"error": str(e)}, status=409)
        except protocol_catalog.CatalogValidationError as e:
            return web.json_response({"error": "invalid catalog", "errors": e.errors},
                                     status=400)
        except OSError as e:
            logger.error(f"Cannot save protocol catalog: {e}")
            return web.json_response({"error": f"cannot write catalog: {e}"}, status=500)
        logger.info(f"Protocol catalog saved by user={self._actor(request)}")
        changed = await asyncio.to_thread(sm.reapply_catalog_sync)
        return web.json_response({"ok": True, "version": version, "restudied": changed})

    async def _rhythm_credentials(self, sm: StorageManager) -> tuple[str, str, Optional[str]]:
        """(base_url, token, site_code): the saved settings file first (a token
        saved but not yet applied by a restart already works), else the
        running RhythmStorage. Empty strings when not configured."""
        saved: dict = {}
        if self.settings_editor is not None:
            try:
                _raw, doc = await asyncio.to_thread(self.settings_editor.read_document)
                saved = ((doc.get("storage") or {}).get("remote_storage") or {}) \
                    .get("rhythm") or {}
            except (OSError, yaml.YAMLError, settings_editor.SettingsError) as e:
                logger.warning(f"Cannot read saved RHYTHM settings: {e}")
        rs = sm.rhythm_storage
        base = str(saved.get("base_url") or rs.base_url or "").strip()
        token = str(saved.get("auth_token") or rs.auth_token or "").strip()
        site = saved.get("site_code") or rs.site_code
        return base, token, site

    async def handle_protocols_rhythm_import(self, request: web.Request) -> web.Response:
        """Read the site's scanner profiles + protocols from RHYTHM and return
        a plan for mapping them onto the catalog. Writes nothing: the page
        applies the ticked proposals as unsaved edits, saved via PUT."""
        self._require_admin(request)
        sm: StorageManager = request.app['storage_manager']
        base, token, site = await self._rhythm_credentials(sm)
        if not base or not token:
            return web.json_response(
                {"error": "Set the RHYTHM base URL and API token on Settings first"},
                status=409)
        try:
            remote = await rhythm_storage_mod.fetch_site_catalog(base, token)
        except rhythm_storage_mod.RhythmApiError as e:
            return web.json_response({"error": e.message, "rhythm_status": e.status},
                                     status=502)
        catalog = sm.protocol_catalog
        doc = await asyncio.to_thread(catalog.document)
        vocab = {"region_indications": options.REGION_INDICATIONS,
                 "iv_contrast": options.IV_CONTRAST}
        plan = rhythm_import.plan_import(doc, remote["profiles"], remote["protocols"],
                                         vocab, site_code=site)
        logger.info(f"Audit: user={self._actor(request)} action=rhythm_import_preview "
                    f"profiles={len(remote['profiles'])} protocols={len(remote['protocols'])}")
        return web.json_response({
            "rhythm": {"base_url": base, "profiles": len(remote["profiles"]),
                       "protocols": len(remote["protocols"])},
            "plan": plan,
            "catalog_version": catalog.version,
        })

    async def handle_protocols_unmatched(self, request: web.Request) -> web.Response:
        sm: StorageManager = request.app['storage_manager']
        return web.json_response({"unmatched": await asyncio.to_thread(
            sm.unmatched_protocols_sync)})

    # ------------------------------------------------------------ admin: settings
    def _editor(self) -> settings_editor.SettingsEditor:
        if self.settings_editor is None:
            raise web.HTTPConflict(
                text=json.dumps({"error": "no editable settings file: the server was not "
                                          "started from config/settings.yaml"}),
                content_type="application/json")
        return self.settings_editor

    async def handle_settings_page(self, request: web.Request) -> web.Response:
        user = request.get("user")
        if user is None or not user.admin:
            raise web.HTTPFound("/")
        return await self._serve_template("settings.html")

    async def handle_settings_get(self, request: web.Request) -> web.Response:
        self._require_admin(request)
        editor = self._editor()
        try:
            snap = await asyncio.to_thread(editor.snapshot)
        except (OSError, yaml.YAMLError, settings_editor.SettingsError) as e:
            logger.error(f"Cannot read settings file: {e}")
            return web.json_response({"error": f"cannot read settings: {e}"}, status=500)
        return web.json_response(snap)

    async def handle_settings_put(self, request: web.Request) -> web.Response:
        """``{"changes": {"<dotted.path>": value, ...}, "version": "<from GET>"}``.

        Only whitelisted fields; 400 with ``errors`` (keyed by path) when
        invalid, 409 if the file changed since it was read.
        """
        self._require_admin(request)
        editor = self._editor()
        body = await self._json_body(request)
        try:
            result = await asyncio.to_thread(
                editor.apply_changes, body.get("changes"), body.get("version"))
        except settings_editor.SettingsConflictError as e:
            return web.json_response({"error": str(e)}, status=409)
        except settings_editor.SettingsValidationError as e:
            return web.json_response({"error": "invalid settings", "errors": e.errors},
                                     status=400)
        except (OSError, yaml.YAMLError, settings_editor.SettingsError) as e:
            logger.error(f"Cannot save settings: {e}")
            return web.json_response({"error": f"cannot save settings: {e}"}, status=500)
        if result.changed:
            # Paths only — values may be secrets.
            logger.info(f"Audit: user={self._actor(request)} action=settings_change "
                        f"paths={result.changed} backup={result.backup}")
        return web.json_response({
            "ok": True, "changed": result.changed, "applied_live": result.applied_live,
            "restart_required": result.restart_required, "version": result.version,
        })

    async def handle_settings_rhythm_test(self, request: web.Request) -> web.Response:
        """Probe RHYTHM with the given (or else the saved) base URL and token."""
        self._require_admin(request)
        editor = self._editor()
        body = await self._json_body(request) if request.can_read_body else {}
        try:
            _raw, doc = await asyncio.to_thread(editor.read_document)
        except (OSError, yaml.YAMLError, settings_editor.SettingsError) as e:
            return web.json_response({"error": f"cannot read settings: {e}"}, status=500)
        saved = ((doc.get("storage") or {}).get("remote_storage") or {}).get("rhythm") or {}
        base_url = str(body.get("base_url") or saved.get("base_url") or "").strip()
        token = str(body.get("auth_token") or saved.get("auth_token") or "").strip()
        if not base_url or not token:
            return web.json_response({"ok": False, "status": None,
                                      "message": "Base URL and API token are both needed"})
        if not re.match(r"^https?://[^\s/]+", base_url):
            return web.json_response({"ok": False, "status": None,
                                      "message": "Base URL must start with http:// or https://"})
        sm: StorageManager = request.app['storage_manager']
        uuids = [p.rhythm_uuid for p in sm.protocol_catalog.entries if not p.uuid_pending]
        return web.json_response(
            await rhythm_storage_mod.probe_connection(base_url, token, uuids))

    async def handle_settings_rhythm_login(self, request: web.Request) -> web.Response:
        """``{"base_url", "username", "password"}``: sign in to RHYTHM.

        Either the token comes back at once (no 2FA) and is saved, or a
        ``pending`` id is returned for ``/rhythm-login/verify``. The token is
        never sent to the browser.
        """
        self._require_admin(request)
        editor = self._editor()
        body = await self._json_body(request)
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        try:
            base_url = settings_editor.validate_value(
                settings_editor.FIELDS_BY_PATH["storage.remote_storage.rhythm.base_url"],
                str(body.get("base_url") or "").strip())
        except ValueError as e:
            return web.json_response({"ok": False, "message": f"Base URL {e}"}, status=400)
        if not base_url or not username or not password:
            return web.json_response(
                {"ok": False, "message": "Base URL, username and password are all needed"},
                status=400)
        result = await rhythm_storage_mod.rhythm_login(base_url, username, password)
        admin = self._actor(request) or "?"
        if result.get("needs_2fa"):
            now = time.time()
            self._rhythm_logins = {k: v for k, v in self._rhythm_logins.items()
                                   if v[3] > now and v[0] != admin}
            pending = secrets.token_urlsafe(16)
            self._rhythm_logins[pending] = (admin, base_url, result["jar"],
                                            now + RHYTHM_LOGIN_TTL_S)
            return web.json_response({"ok": True, "needs_2fa": True, "pending": pending,
                                      "message": result["message"]})
        if not result.get("ok"):
            return web.json_response({"ok": False, "message": result.get("message")})
        return await self._save_rhythm_token(request, editor, base_url, result)

    async def handle_settings_rhythm_verify(self, request: web.Request) -> web.Response:
        """``{"pending", "code"}``: finish a RHYTHM sign-in with the 2FA code."""
        self._require_admin(request)
        editor = self._editor()
        body = await self._json_body(request)
        pending = str(body.get("pending") or "")
        code = str(body.get("code") or "").strip().replace(" ", "")
        entry = self._rhythm_logins.get(pending)
        if (entry is None or entry[3] < time.time()
                or entry[0] != (self._actor(request) or "?")):
            self._rhythm_logins.pop(pending, None)
            return web.json_response(
                {"ok": False, "message": "This sign-in expired — start again"}, status=400)
        if not code:
            return web.json_response({"ok": False, "message": "Enter the code"}, status=400)
        _admin, base_url, jar, _exp = entry
        result = await rhythm_storage_mod.rhythm_verify_2fa(base_url, jar, code)
        if not result.get("ok"):
            # RHYTHM drops the pending login after a few wrong codes; keep ours
            # until it expires so a typo can be retried.
            return web.json_response({"ok": False, "message": result.get("message")})
        self._rhythm_logins.pop(pending, None)
        return await self._save_rhythm_token(request, editor, base_url, result)

    async def _save_rhythm_token(self, request: web.Request,
                                 editor: settings_editor.SettingsEditor,
                                 base_url: str, result: dict) -> web.Response:
        changes = {"storage.remote_storage.rhythm.base_url": base_url,
                   "storage.remote_storage.rhythm.auth_token": result["token"]}
        try:
            snap = await asyncio.to_thread(editor.snapshot)
            saved = await asyncio.to_thread(editor.apply_changes, changes, snap["version"])
        except settings_editor.SettingsValidationError as e:
            return web.json_response({"ok": False, "message": "; ".join(e.errors.values())},
                                     status=400)
        except (OSError, yaml.YAMLError, settings_editor.SettingsError) as e:
            logger.error(f"Cannot save the RHYTHM token: {e}")
            return web.json_response({"ok": False, "message": f"cannot save settings: {e}"},
                                     status=500)
        logger.info(f"Audit: user={self._actor(request)} action=rhythm_token_obtained "
                    f"rhythm_user={result.get('username')!r} paths={saved.changed}")
        return web.json_response({"ok": True, "username": result.get("username"),
                                  "restart_required": saved.restart_required})

    # ------------------------------------------------------------ protocol draft
    @staticmethod
    def _bundled_choices() -> dict:
        try:
            return json.loads(_RHYTHM_CHOICES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.error(f"Cannot read {_RHYTHM_CHOICES_FILE}", exc_info=True)
            return {"categories": []}

    async def _draft_choices(self, base: str, token: str
                             ) -> tuple[protocol_draft.Choices, str, Optional[str]]:
        """RHYTHM's live protocol choices when reachable, else the bundled
        snapshot. Vendor auto-kV/mA lists and mA input labels always come
        from the snapshot (RHYTHM's API doesn't serve them)."""
        bundled = await asyncio.to_thread(self._bundled_choices)
        cats, source, err = bundled.get("categories") or [], "offline", None
        if base and token:
            try:
                cats = await rhythm_storage_mod.fetch_choices(base, token)
                source = "live"
            except rhythm_storage_mod.RhythmApiError as e:
                err = e.message
        return (protocol_draft.Choices(cats, bundled.get("manufacturer_field_options"),
                                       bundled.get("ma_modulation_inputs")), source, err)

    @staticmethod
    def _study_number(request: web.Request) -> int:
        try:
            return int(request.match_info["n"])
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid study number"}),
                                     content_type="application/json")

    async def handle_protocol_draft_get(self, request: web.Request) -> web.Response:
        """A pre-filled local + RHYTHM protocol drafted from study {n}."""
        self._require_admin(request)
        n = self._study_number(request)
        sm: StorageManager = request.app['storage_manager']
        try:
            inputs = await asyncio.to_thread(sm.protocol_draft_inputs_sync, n)
        except Exception as e:  # noqa: BLE001 - corrupt sidecar: report, don't 500
            logger.warning(f"Protocol draft for study {n}: {e}", exc_info=True)
            return web.json_response({"error": "this study's exam record is unreadable — "
                                               "re-extract it first"}, status=422)
        if inputs is None:
            return web.json_response({"error": "this study has no exam record yet"},
                                     status=404)
        exam, headers = inputs
        base, token, _site = await self._rhythm_credentials(sm)
        choices, source, choices_err = await self._draft_choices(base, token)
        doc = await asyncio.to_thread(sm.protocol_catalog.document)
        draft = protocol_draft.draft_from_exam(exam, headers, choices, doc, n)

        profiles: list[dict] = []
        profile_match = None
        existing: list[dict] = []
        rhythm_error = choices_err
        if base and token:
            try:
                profiles = await rhythm_storage_mod.fetch_all(
                    base, token, rhythm_storage_mod.PROFILES_PATH)
                probe = {"manufacturer": headers.get("Manufacturer")
                         or (exam.get("scanner") or {}).get("manufacturer"),
                         "model": headers.get("ManufacturerModelName")
                         or (exam.get("scanner") or {}).get("model")}
                exact = [p["id"] for p in profiles
                         if rhythm_import._scanner_score(probe, p) == 2]
                profile_match = exact[0] if len(exact) == 1 else None
                ptype = draft["rhythm"]["fields"]["protocol_type"]["value"]
                if profile_match:
                    found = await rhythm_storage_mod.find_protocols(
                        base, token, profile_match, ptype)
                    want = rhythm_import.protocol_key(draft["rhythm"]["protocol_name"])
                    ag = draft["rhythm"]["fields"]["age_group"]["value"]
                    existing = [{"id": p.get("id"), "protocol_name": p.get("protocol_name"),
                                 "protocol_type": p.get("protocol_type"),
                                 "age_group": p.get("age_group")}
                                for p in found
                                if rhythm_import.protocol_key(p.get("protocol_name")) == want
                                or (ag and p.get("age_group") == ag)]
            except rhythm_storage_mod.RhythmApiError as e:
                rhythm_error = e.message
        return web.json_response({
            "study": n, "draft": draft, "choices": choices.to_dict(),
            "choices_source": source, "rhythm_configured": bool(base and token),
            "rhythm_base_url": base or None, "rhythm_error": rhythm_error,
            "profiles": [{"id": p.get("id"), "manufacturer": p.get("manufacturer_name"),
                          "model": p.get("scanner_model_name"),
                          "detector_rows": p.get("detector_rows")} for p in profiles],
            "profile_match": profile_match, "existing": existing,
            "catalog_version": sm.protocol_catalog.version,
            "region_indications": options.REGION_INDICATIONS,
            "iv_contrast": options.IV_CONTRAST,
            "local_scanners": [{"id": sc.get("id"), "name": sc.get("name")
                                or f"{sc.get('manufacturer', '')} {sc.get('model', '')}".strip()
                                or "Any scanner"} for sc in doc.get("scanners") or []],
        })

    @staticmethod
    def _validate_local(local: Any, doc: dict) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not isinstance(local, dict):
            return {"local": "missing local protocol"}
        names = [str(x).strip() for x in local.get("names") or [] if str(x).strip()]
        if not names:
            errors["local.names"] = "At least one matching name is needed"
        sid = local.get("scanner_id")
        new = local.get("new_scanner") or {}
        if sid:
            if not any(s.get("id") == sid for s in doc.get("scanners") or []):
                errors["local.scanner_id"] = "Unknown local scanner"
        elif not (str(new.get("manufacturer") or "").strip()
                  and str(new.get("model") or "").strip()):
            errors["local.scanner_id"] = "Choose a local scanner or give manufacturer + model"
        region, ci = local.get("region"), local.get("clinical_indication")
        if region or ci:
            ok = any(c["region"] == region and c["indication"] == ci
                     for c in options.REGION_INDICATIONS)
            if not ok:
                errors["local.region"] = "Not one of RHYTHM's region/indication combinations"
        iv = local.get("iv_contrast")
        if iv and iv not in options.IV_CONTRAST:
            errors["local.iv_contrast"] = "Unknown contrast value"
        return errors

    @staticmethod
    def _apply_local(doc: dict, local: dict, rhythm_id: str) -> tuple[dict, str]:
        """Put the protocol (with *rhythm_id*) into catalog *doc*; returns
        (doc, protocol key). An existing protocol with one of the names on
        that scanner gets the UUID (and empty fields filled) instead."""
        scanners = doc.setdefault("scanners", [])
        sid = local.get("scanner_id")
        scanner = next((s for s in scanners if s.get("id") == sid), None) if sid else None
        if scanner is None:
            new = local.get("new_scanner") or {}
            taken = {s.get("id") for s in scanners}
            base_id = protocol_catalog.slugify(
                f"{new.get('manufacturer', '')} {new.get('model', '')}") or "scanner"
            new_id, i = base_id, 2
            while new_id in taken:
                new_id, i = f"{base_id}-{i}", i + 1
            scanner = {"id": new_id,
                       "name": new.get("name") or f"{new.get('manufacturer')} {new.get('model')}",
                       "manufacturer": new.get("manufacturer"), "model": new.get("model"),
                       "rhythm_scanner_id": None, "protocols": []}
            scanners.append(scanner)
        protos = scanner.setdefault("protocols", [])
        names = [str(x).strip() for x in local.get("names") or [] if str(x).strip()]
        wanted = {protocol_catalog.normalize_name(x) for x in names}
        hit = next((p for p in protos
                    if wanted & {protocol_catalog.normalize_name(x)
                                 for x in p.get("names") or []}), None)
        fields = {"region": local.get("region") or None,
                  "clinical_indication": local.get("clinical_indication") or None,
                  "iv_contrast": local.get("iv_contrast") or None}
        if hit is not None:
            hit["rhythm_uuid"] = rhythm_id
            for k, v in fields.items():
                if v and not hit.get(k):
                    hit[k] = v
            return doc, hit["key"]
        taken = {p.get("key") for s in scanners for p in s.get("protocols") or []}
        key_base = protocol_catalog.slugify(local.get("key") or names[0]) or "protocol"
        key, i = key_base, 2
        while key in taken:
            key, i = f"{key_base}-{i}", i + 1
        protos.append({"key": key, "label": local.get("label") or names[0], "names": names,
                       "rhythm_uuid": rhythm_id, **fields, "notes": ""})
        return doc, key

    async def handle_protocol_draft_post(self, request: web.Request) -> web.Response:
        """Create the RHYTHM protocol (or link ``use_existing``) and add the
        local protocol with its UUID. Everything is validated first; RHYTHM is
        called only when the whole submission is valid."""
        self._require_admin(request)
        n = self._study_number(request)
        sm: StorageManager = request.app['storage_manager']
        body = await self._json_body(request)
        base, token, _site = await self._rhythm_credentials(sm)
        if not base or not token:
            return web.json_response(
                {"error": "Set the RHYTHM base URL and API token on Settings first"},
                status=409)
        catalog = sm.protocol_catalog
        doc = await asyncio.to_thread(catalog.document)
        local = body.get("local") or {}
        errors = self._validate_local(local, doc)
        use_existing = str(body.get("use_existing") or "").strip() or None
        payload: dict = {}
        if use_existing:
            if protocol_catalog.is_pending_uuid(use_existing) or not re.fullmatch(
                    r"[0-9a-fA-F-]{32,36}", use_existing):
                errors["use_existing"] = "Not a protocol UUID"
        else:
            choices, _src, _err = await self._draft_choices(base, token)
            payload, rerr = protocol_draft.validate_submission(body.get("rhythm") or {},
                                                               choices)
            errors.update({f"rhythm.{k}": v for k, v in rerr.items()})
        if errors:
            return web.json_response({"error": "invalid draft", "errors": errors}, status=400)

        warnings: list[str] = []
        if use_existing:
            rhythm_id = use_existing
        else:
            try:
                record = await rhythm_storage_mod.create_protocol(base, token, payload)
            except rhythm_storage_mod.RhythmApiError as e:
                errs = getattr(e, "errors", {}) or {}
                return web.json_response(
                    {"error": e.message, "errors": {f"rhythm.{k}": v for k, v in errs.items()}},
                    status=400 if e.status == 400 else 502)
            rhythm_id = str(record["id"])
            logger.info(f"Audit: user={self._actor(request)} action=rhythm_protocol_created "
                        f"study={n} rhythm_id={rhythm_id}")
            back = await rhythm_storage_mod.get_protocol(base, token, rhythm_id) or record
            for k in ("examination_group", "age_group", "protocol_type"):
                if payload.get(k) and back.get(k) != payload[k] and k in back:
                    warnings.append(f"RHYTHM stored {k} = {back.get(k)!r}")
            if payload.get("examination_group") and "examination_group" not in back:
                warnings.append("RHYTHM's API did not store the examination group — set it on "
                                "RHYTHM's Protocol Records page for this protocol")

        try:
            new_doc, key = self._apply_local(doc, local, rhythm_id)
            await asyncio.to_thread(catalog.replace, new_doc,
                                    expected_version=catalog.version)
        except (protocol_catalog.CatalogError, OSError) as e:
            logger.error(f"RHYTHM protocol {rhythm_id} created but the local catalog was not "
                         f"saved: {e}")
            detail = getattr(e, "errors", None) or [str(e)]
            return web.json_response({
                "ok": False, "rhythm_id": rhythm_id,
                "error": "Created on RHYTHM, but the local protocol could not be saved — add "
                         f"UUID {rhythm_id} on the Protocols page",
                "details": detail, "warnings": warnings}, status=200)
        restudied = await asyncio.to_thread(sm.reapply_catalog_sync)
        logger.info(f"Audit: user={self._actor(request)} action=protocol_drafted study={n} "
                    f"local_key={key} rhythm_id={rhythm_id}")
        return web.json_response({
            "ok": True, "rhythm_id": rhythm_id, "local_key": key, "restudied": restudied,
            "rhythm_url": f"{base}/protocols/records/", "warnings": warnings,
            "linked_existing": bool(use_existing)})

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
        self.setup.startup()

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
            "scanners": options.scanner_options(sm.protocol_catalog),
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
                # protocol keys from the protocol catalog (config/protocols.yaml), for
                # the exam panel's "RHYTHM protocol" dropdown
                "rhythm_protocols": [
                    {"id": e.id, "name": e.name, "description": e.description,
                     "uuid_pending": e.uuid_pending, "scanner_id": e.scanner_id}
                    for e in storage_manager.protocol_catalog.entries
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
