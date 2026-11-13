"""First-run setup wizard (``/setup``).

Two situations make setup *required*:

* **No users exist.** Nobody can log in, so the wizard is reachable without a
  login, but every call must carry the one-time **setup code** (header
  ``X-Setup-Code``). The code is generated at startup, logged at WARNING and
  written to ``config/.setup_code`` (0600), so only someone with access to
  the server console, log or filesystem can claim the first admin account.
* **Users exist but setup was never completed** (e.g. the installer created
  the admin). Then the wizard needs an admin session instead of the code, and
  admins' browser visits to ``/`` and ``/studies`` are sent to it once.

Finishing (or skipping) writes ``config/.setup_complete``; from then on
``/setup`` and ``/api/setup/*`` are 404. A server that already has users and
a ``settings.yaml`` when it starts (an existing deployment) is marked
complete silently.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from aiohttp import web

from pixieveil.auth import passwords
from pixieveil.auth.user_store import UserStoreError, normalize_username
from pixieveil.config import settings_editor as se
from pixieveil.storage import rhythm_storage

if TYPE_CHECKING:
    from pixieveil.dashboard.server import Dashboard

logger = logging.getLogger(__name__)

CODE_HEADER = "X-Setup-Code"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I
CODE_LEN = 8
MAX_FAILURES = 5
LOCKOUT_S = 300.0
MARKER_NAME = ".setup_complete"
CODE_FILE_NAME = ".setup_code"
EXAMPLE_NAME = "settings.example.yaml"

# Fields the wizard offers, in step order (all also validated server-side).
WIZARD_PATHS = [
    "dicom_server.ae_title", "dicom_server.port", "anonymization.profile",
    "storage.remote_storage.rhythm.base_url", "storage.remote_storage.rhythm.auth_token",
    "storage.remote_storage.rhythm.site_code",
    "defacing.enabled", "defacing.device", "defacing.keep_backup",
    "storage.retain_after_export_days", "storage.max_storage_gb",
]
# Wizard defaults that differ from the plain settings defaults.
_WIZARD_DEFAULTS = {"anonymization.profile": "rhythm", "defacing.keep_backup": False}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def settings_target(source: Optional[Path]) -> Optional[Path]:
    """The settings.yaml the wizard writes: the file the server was started
    from, or settings.yaml next to the example it fell back to."""
    if source is None:
        return None
    source = Path(source)
    return source.with_name("settings.yaml") if source.name == EXAMPLE_NAME else source


class SetupState:
    """Marker, setup code and lockout for one dashboard."""

    def __init__(self, dashboard: "Dashboard", config_dir: Path):
        self.d = dashboard
        self.config_dir = Path(config_dir)
        self.marker = self.config_dir / MARKER_NAME
        self.code_file = self.config_dir / CODE_FILE_NAME
        source = dashboard.settings.source_path
        self.settings_path = settings_target(source)
        self.example_path = (Path(source).with_name(EXAMPLE_NAME) if source is not None
                             else None)
        self._code: Optional[str] = None
        self._failures: dict[str, tuple[int, float]] = {}
        self._complete = self.marker.exists()

    # ---- state
    def startup(self) -> None:
        """Called once at Dashboard start: mark an existing deployment
        complete, or announce the setup code when there is no user yet."""
        if self._complete:
            return
        users = self.d.user_store.count()
        if users and (self.settings_path is None or self.settings_path.exists()):
            self.complete("existing-deployment")
            return
        if users == 0:
            self.announce_code()

    @property
    def required(self) -> bool:
        if self._complete:
            return False
        if self.marker.exists():
            self._complete = True
            return False
        return True

    @property
    def needs_admin(self) -> bool:
        return self.d.user_store.count() == 0

    def complete(self, by: str) -> None:
        _write_private(self.marker, json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(), "by": by}) + "\n")
        self._complete = True
        self._code = None
        try:
            self.code_file.unlink()
        except FileNotFoundError:
            pass
        logger.info(f"First-run setup complete (by={by})")

    # ---- setup code
    def announce_code(self) -> str:
        if self._code is None:
            raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            self._code = f"{raw[:4]}-{raw[4:]}"
            try:
                _write_private(self.code_file, self._code + "\n")
            except OSError as e:
                logger.error(f"Cannot write {self.code_file}: {e}")
            logger.warning(
                "No dashboard users yet. Open http://<this server>:%s/setup and enter "
                "the setup code %s (also in %s)",
                self.d.settings.http_server.get("port", 8070), self._code, self.code_file)
        return self._code

    def _locked(self, ip: str) -> float:
        _n, until = self._failures.get(ip, (0, 0.0))
        return max(0.0, until - time.time())

    def check_code(self, ip: str, presented: Optional[str]) -> Optional[web.Response]:
        """None when the code is right; otherwise the error response."""
        left = self._locked(ip)
        if left:
            return web.json_response(
                {"error": f"Too many wrong codes — try again in {int(left // 60) + 1} min"},
                status=429)
        expected = self.announce_code()
        given = (presented or "").strip().upper().replace(" ", "")
        if len(given) == CODE_LEN:
            given = f"{given[:4]}-{given[4:]}"
        if hmac.compare_digest(given.encode(), expected.encode()):
            self._failures.pop(ip, None)
            return None
        n, _until = self._failures.get(ip, (0, 0.0))
        n += 1
        until = time.time() + LOCKOUT_S if n >= MAX_FAILURES else 0.0
        self._failures[ip] = (0 if until else n, until)
        logger.warning(f"Wrong setup code from ip={ip}")
        return web.json_response({"error": "Wrong setup code"}, status=403)


# ------------------------------------------------------------------ routes
class SetupRoutes:
    """Handlers for /setup and /api/setup/*; the middleware lets them through
    unauthenticated only while setup is required, and each enforces its own
    gate (setup code before the first admin exists, admin session after)."""

    def __init__(self, dashboard: "Dashboard", state: SetupState):
        self.d = dashboard
        self.state = state

    def routes(self) -> list:
        return [
            web.get("/setup", self.page),
            web.get("/api/setup", self.status),
            web.post("/api/setup/verify-code", self.verify_code),
            web.post("/api/setup/rhythm-test", self.rhythm_test),
            web.post("/api/setup/finish", self.finish),
            web.post("/api/setup/skip", self.skip),
        ]

    # ---- gates
    def _csrf(self, request: web.Request) -> Optional[web.Response]:
        from pixieveil.dashboard.server import CSRF_HEADER, CSRF_VALUE
        if (request.headers.get(CSRF_HEADER) != CSRF_VALUE
                or not self.d._same_origin(request)):
            return web.json_response({"error": "CSRF check failed"}, status=403)
        return None

    def _gate(self, request: web.Request) -> tuple[Optional[web.Response], Optional[Any]]:
        """(error, admin_user). Code gate while no user exists, else admin."""
        if not self.state.required:
            raise web.HTTPNotFound()
        if self.state.needs_admin:
            err = self.state.check_code(request.remote or "?",
                                        request.headers.get(CODE_HEADER))
            return err, None
        user, _via = self.d._resolve_user(request)
        if user is None:
            return web.json_response({"error": "log in as an admin to finish setup"},
                                     status=401), None
        if not user.admin:
            return web.json_response({"error": "admin only"}, status=403), None
        return None, user

    # ---- data
    def _editor(self) -> Optional[se.SettingsEditor]:
        if self.state.settings_path is None:
            return None
        sm = self.d.app['storage_manager']
        return se.SettingsEditor(
            self.state.settings_path, self.d.settings,
            catalog_site_code=lambda: sm.protocol_catalog.site_code)

    def _current_doc(self) -> dict:
        for path in (self.state.settings_path, self.state.example_path):
            if path is not None and path.exists():
                try:
                    return se.SettingsEditor(path).read_document()[1]
                except (OSError, se.SettingsError, ValueError):
                    logger.warning(f"Cannot read {path} for setup prefill", exc_info=True)
        return {}

    def _values(self) -> list[dict]:
        doc = self._current_doc()
        # A fresh install prefills from the example file; there the wizard's
        # recommended values replace the example's.
        fresh = self.state.settings_path is None or not self.state.settings_path.exists()
        out = []
        for path in WIZARD_PATHS:
            f = se.SETUP_FIELDS_BY_PATH[path]
            present, value = se._get(doc, path)
            item = {"path": path, "label": f.label, "kind": f.kind, "help": f.help,
                    "choices": list(f.choices), "min": f.minimum, "max": f.maximum,
                    "nullable": f.nullable}
            if f.kind == "secret":
                item["value"], item["is_set"] = None, bool(present and value)
            else:
                item["value"] = value if present else _WIZARD_DEFAULTS.get(path, f.default)
                if fresh and path in _WIZARD_DEFAULTS:
                    item["value"] = _WIZARD_DEFAULTS[path]
            out.append(item)
        return out

    @staticmethod
    def _defacing_facts(dashboard: "Dashboard") -> dict:
        sm = dashboard.app['storage_manager']
        facts: dict[str, Any] = {"cuda": None, "torch": False, "model_present": None}
        try:
            import torch  # noqa: F401 - optional heavy dependency
            facts["torch"] = True
            facts["cuda"] = bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001 - torch missing or broken: report, don't fail
            pass
        model_dir = getattr(sm.defacer, "model_dir", None)
        if model_dir is not None:
            facts["model_present"] = (Path(model_dir) / "Dataset001_DEFACE").exists()
        return facts

    # ---- handlers
    async def page(self, request: web.Request) -> web.Response:
        if not self.state.required:
            raise web.HTTPNotFound()
        if not self.state.needs_admin:
            user, _ = self.d._resolve_user(request)
            if user is None:
                raise web.HTTPFound("/login?next=%2Fsetup")
            if not user.admin:
                raise web.HTTPFound("/")
        return await self.d._serve_template("setup.html")

    async def status(self, request: web.Request) -> web.Response:
        if not self.state.required:
            raise web.HTTPNotFound()
        body: dict[str, Any] = {
            "required": True,
            "needs_code": self.state.needs_admin,
            "needs_admin": self.state.needs_admin,
            "editable": self.state.settings_path is not None,
            "settings_path": str(self.state.settings_path or ""),
        }
        # Values only once the caller passed the gate.
        authorized = False
        if self.state.needs_admin:
            if request.headers.get(CODE_HEADER):
                err = self.state.check_code(request.remote or "?",
                                            request.headers.get(CODE_HEADER))
                if err is not None:
                    return err
                authorized = True
        else:
            user, _ = self.d._resolve_user(request)
            authorized = bool(user and user.admin)
            body["user"] = user.username if user else None
        if authorized:
            body["fields"] = await asyncio.to_thread(self._values)
            body["defacing"] = await asyncio.to_thread(self._defacing_facts, self.d)
        return web.json_response(body)

    async def verify_code(self, request: web.Request) -> web.Response:
        err = self._csrf(request)
        if err is not None:
            return err
        err, _user = self._gate(request)
        return err if err is not None else web.json_response({"ok": True})

    async def rhythm_test(self, request: web.Request) -> web.Response:
        err = self._csrf(request)
        if err is None:
            err = self._gate(request)[0]
        if err is not None:
            return err
        body = await self.d._json_body(request)
        doc = await asyncio.to_thread(self._current_doc)
        saved = ((doc.get("storage") or {}).get("remote_storage") or {}).get("rhythm") or {}
        base_url = str(body.get("base_url") or saved.get("base_url") or "").strip()
        token = str(body.get("auth_token") or saved.get("auth_token") or "").strip()
        if not base_url or not token:
            return web.json_response({"ok": False, "status": None,
                                      "message": "Base URL and API token are both needed"})
        try:
            base_url = se.validate_value(
                se.FIELDS_BY_PATH["storage.remote_storage.rhythm.base_url"], base_url)
        except ValueError as e:
            return web.json_response({"ok": False, "status": None, "message": f"Base URL {e}"})
        sm = self.d.app['storage_manager']
        uuids = [p.rhythm_uuid for p in sm.protocol_catalog.entries if not p.uuid_pending]
        return web.json_response(
            await rhythm_storage.probe_connection(base_url, token, uuids))

    async def skip(self, request: web.Request) -> web.Response:
        err = self._csrf(request)
        if err is not None:
            return err
        if self.state.required and self.state.needs_admin:
            return web.json_response(
                {"error": "create the first admin before skipping setup"}, status=409)
        err, user = self._gate(request)
        if err is not None:
            return err
        await asyncio.to_thread(self.state.complete, user.username)
        return web.json_response({"ok": True})

    async def finish(self, request: web.Request) -> web.Response:
        """``{"admin": {"username", "password"}?, "changes": {path: value}}``.

        Everything is validated before anything is written: an invalid field
        or account leaves no admin, no settings change and no marker behind.
        """
        err = self._csrf(request)
        if err is not None:
            return err
        err, user = self._gate(request)
        if err is not None:
            return err
        body = await self.d._json_body(request)
        changes = body.get("changes") or {}
        if not isinstance(changes, dict):
            return web.json_response({"error": "changes must be an object"}, status=400)
        changes = {k: v for k, v in changes.items()
                   if not (se.SETUP_FIELDS_BY_PATH.get(k) and
                           se.SETUP_FIELDS_BY_PATH[k].kind == "secret" and v in ("", None))}

        errors: dict[str, str] = {}
        username = password = None
        if self.state.needs_admin:
            admin = body.get("admin") or {}
            try:
                username = normalize_username(str(admin.get("username") or ""))
            except UserStoreError as e:
                errors["admin.username"] = str(e)
            password = str(admin.get("password") or "")
            perr = passwords.validate_new_password(password)
            if perr:
                errors["admin.password"] = perr

        editor = self._editor()
        normalized: dict[str, Any] = {}
        if changes:
            if editor is None:
                return web.json_response(
                    {"error": "this server was not started from a settings file; "
                              "edit settings by hand"}, status=409)
            try:
                normalized = editor.validate_changes(changes, se.SETUP_FIELDS_BY_PATH)
            except se.SettingsValidationError as e:
                errors.update(e.errors)
        if errors:
            return web.json_response({"error": "invalid setup", "errors": errors}, status=400)

        restart: list[str] = []
        try:
            if normalized:
                assert editor is not None
                if self.state.example_path is not None:
                    await asyncio.to_thread(
                        se.ensure_file, editor.path, self.state.example_path)
                version = (await asyncio.to_thread(editor.snapshot))["version"]
                result = await asyncio.to_thread(
                    editor.apply_changes, changes, version, se.SETUP_FIELDS_BY_PATH)
                restart = result.restart_required
                if self.d.settings_editor is None:
                    self.d.settings_editor = editor
                else:
                    self.d.settings_editor.restart_pending.update(result.restart_required)
            if self.state.needs_admin:
                user = await asyncio.to_thread(
                    self.d.user_store.add, username, password, True)
        except se.SettingsValidationError as e:
            return web.json_response({"error": "invalid setup", "errors": e.errors},
                                     status=400)
        except (OSError, se.SettingsError, UserStoreError) as e:
            logger.error(f"Setup could not be saved: {e}")
            return web.json_response({"error": f"setup could not be saved: {e}"}, status=500)

        await asyncio.to_thread(self.state.complete, user.username)
        logger.info(f"Audit: user={user.username} action=setup_finish "
                    f"paths={sorted(normalized)}")
        resp = web.json_response({"ok": True, "username": user.username,
                                  "restart_required": restart})
        self.d._set_session_cookie(resp, user)
        return resp
