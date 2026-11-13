"""The /settings page backend: whitelisted fields, validation, atomic save
with backup, live vs restart apply, secrets never echoed, admin-only routes,
and the RHYTHM connection probe (against a fake aiohttp session)."""
import asyncio
import logging
import stat

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from pixieveil.config import Settings, settings_editor as se
from pixieveil.dashboard.server import Dashboard
from pixieveil.storage import rhythm_storage
from pixieveil.storage.storage_manager import StorageManager

TOKEN = "super-secret-rhythm-token"
CSRF = {"X-Requested-With": "PixieVeil"}


def _write_settings(tmp_path, **extra) -> tuple:
    doc = {
        "dicom_server": {"ae_title": "PV", "port": 4070},
        "storage": {
            "base_path": str(tmp_path / "data"),
            "temp_path": str(tmp_path / "tmp"),
            "retain_after_export_days": 14,
            "remote_storage": {"rhythm": {"base_url": "https://r.example",
                                          "auth_token": TOKEN, "site_code": "S014"}},
        },
        "study": {"completion_timeout": 120},
        "defacing": {"enabled": False, "keep_backup": True},
        "logging": {"anontrail": str(tmp_path / "anontrail.jsonl")},
    }
    doc.update(extra)
    path = tmp_path / "config" / "settings.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(doc))
    return path, Settings.load(path)


def _editor(tmp_path, catalog_site=None):
    path, settings = _write_settings(tmp_path)
    return se.SettingsEditor(path, settings, catalog_site_code=lambda: catalog_site), settings


def _field(snap, path):
    for sec in snap["sections"]:
        for f in sec["fields"]:
            if f["path"] == path:
                return f
    raise KeyError(path)


class TestValidation:
    @pytest.mark.parametrize("path,value,ok", [
        ("storage.retain_after_export_days", 30, 30),
        ("storage.retain_after_export_days", "7", 7),
        ("storage.retain_after_export_days", None, None),
        ("storage.max_storage_gb", 2.5, 2.5),
        ("defacing.enabled", True, True),
        ("defacing.device", "cpu", "cpu"),
        ("series_filter.exclude_modalities", "pr, rt sr", ["PR", "RT", "SR"]),
        ("series_filter.exclude", "ImageType: ^DERIVED\n\n", {"ImageType": "^DERIVED"}),
        ("series_filter.exclude", "", None),
        ("storage.remote_storage.rhythm.base_url", "https://x.org/", "https://x.org"),
    ])
    def test_accepts_and_normalizes(self, path, value, ok):
        assert se.validate_value(se.FIELDS_BY_PATH[path], value) == ok

    @pytest.mark.parametrize("path,value", [
        ("storage.retain_after_export_days", 0),
        ("storage.retain_after_export_days", 1.5),
        ("storage.retain_after_export_days", True),
        ("study.completion_timeout", None),          # not nullable
        ("defacing.enabled", "yes"),
        ("defacing.device", "tpu"),
        ("defacing.mask_dilation_mm", 50),
        ("series_filter.exclude_modalities", ["P R"]),
        ("series_filter.include", "SeriesDescription: (unclosed"),
        ("series_filter.include", "no colon here"),
        ("storage.remote_storage.rhythm.base_url", "ftp://x"),
        ("storage.remote_storage.rhythm.site_code", "a\nb"),
    ])
    def test_rejects(self, path, value):
        with pytest.raises(ValueError):
            se.validate_value(se.FIELDS_BY_PATH[path], value)


class TestEditor:
    def test_snapshot_hides_secret_and_lists_read_only(self, tmp_path):
        ed, _ = _editor(tmp_path)
        snap = ed.snapshot()
        tok = _field(snap, "storage.remote_storage.rhythm.auth_token")
        assert tok["value"] is None and tok["is_set"] is True
        assert TOKEN not in repr(snap)
        assert _field(snap, "defacing.device")["value"] == "cuda"       # default shown
        assert _field(snap, "defacing.device")["is_default"] is True
        ro = {r["path"]: r["value"] for r in snap["read_only"]}
        assert ro["dicom_server.ae_title"] == "PV"

    def test_save_writes_backup_mode_and_keeps_other_keys(self, tmp_path):
        ed, _ = _editor(tmp_path)
        before = yaml.safe_load(ed.path.read_text())
        res = ed.apply_changes({"defacing.enabled": True}, ed.snapshot()["version"])
        assert res.changed == ["defacing.enabled"]
        after = yaml.safe_load(ed.path.read_text())
        assert after["defacing"]["enabled"] is True
        before["defacing"]["enabled"] = True
        assert after == before
        assert stat.S_IMODE(ed.path.stat().st_mode) == 0o600
        backups = list(ed.path.parent.glob("settings.yaml.bak.*"))
        assert len(backups) == 1
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert yaml.safe_load(backups[0].read_text())["defacing"]["enabled"] is False

    def test_backups_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(se, "KEEP_BACKUPS", 3)
        ed, _ = _editor(tmp_path)
        for days in range(1, 7):
            ed.apply_changes({"storage.retain_after_export_days": days},
                             ed.snapshot()["version"])
        assert len(list(ed.path.parent.glob("settings.yaml.bak.*"))) == 3

    def test_stale_version_conflicts(self, tmp_path):
        ed, _ = _editor(tmp_path)
        version = ed.snapshot()["version"]
        ed.path.write_text(ed.path.read_text() + "\n# edited by hand\n")
        with pytest.raises(se.SettingsConflictError):
            ed.apply_changes({"defacing.enabled": True}, version)

    def test_unknown_key_rejected_and_nothing_written(self, tmp_path):
        ed, _ = _editor(tmp_path)
        raw = ed.path.read_bytes()
        with pytest.raises(se.SettingsValidationError) as exc:
            ed.apply_changes({"anonymization.profile": "research", "defacing.enabled": True},
                             ed.snapshot()["version"])
        assert "anonymization.profile" in exc.value.errors
        assert ed.path.read_bytes() == raw

    def test_site_code_must_match_catalog(self, tmp_path):
        ed, _ = _editor(tmp_path, catalog_site="S014")
        with pytest.raises(se.SettingsValidationError) as exc:
            ed.apply_changes({"storage.remote_storage.rhythm.site_code": "S999"},
                             ed.snapshot()["version"])
        assert "storage.remote_storage.rhythm.site_code" in exc.value.errors
        ed.apply_changes({"storage.remote_storage.rhythm.site_code": "s014"},
                         ed.snapshot()["version"])

    def test_secret_kept_unless_sent_and_cleared_by_null(self, tmp_path):
        ed, _ = _editor(tmp_path)
        ed.apply_changes({"storage.remote_storage.rhythm.site_code": "S015"},
                         ed.snapshot()["version"])
        rh = yaml.safe_load(ed.path.read_text())["storage"]["remote_storage"]["rhythm"]
        assert rh["auth_token"] == TOKEN
        ed.apply_changes({"storage.remote_storage.rhythm.auth_token": None},
                         ed.snapshot()["version"])
        rh = yaml.safe_load(ed.path.read_text())["storage"]["remote_storage"]["rhythm"]
        assert "auth_token" not in rh

    def test_live_field_applied_in_memory_restart_field_pending(self, tmp_path):
        ed, settings = _editor(tmp_path)
        res = ed.apply_changes({"storage.retain_after_export_days": None,
                                "study.max_study_size_mb": 500,
                                "defacing.enabled": True},
                               ed.snapshot()["version"])
        assert sorted(res.applied_live) == ["storage.retain_after_export_days",
                                            "study.max_study_size_mb"]
        assert res.restart_required == ["defacing.enabled"]
        assert "retain_after_export_days" not in settings.storage
        assert settings.study["max_study_size_mb"] == 500
        assert settings.defacing["enabled"] is False        # not until restart
        assert ed.snapshot()["restart_pending"] == ["defacing.enabled"]

    def test_live_retention_reaches_storage_manager(self, tmp_path):
        ed, settings = _editor(tmp_path)
        sm = StorageManager(settings)
        ed.apply_changes({"storage.retain_after_export_days": 3}, ed.snapshot()["version"])
        assert sm.settings.storage["retain_after_export_days"] == 3

    def test_unchanged_value_is_not_a_change(self, tmp_path):
        ed, _ = _editor(tmp_path)
        res = ed.apply_changes({"storage.retain_after_export_days": 14},
                               ed.snapshot()["version"])
        assert res.changed == []
        assert not list(ed.path.parent.glob("settings.yaml.bak.*"))


# ------------------------------------------------------------------ routes
def _dashboard(tmp_path) -> Dashboard:
    _path, settings = _write_settings(tmp_path)
    d = Dashboard(settings, StorageManager(settings))
    d.user_store.add("root", "root-password", admin=True)
    d.user_store.add("bob", "bob-password")
    d._admin_token, _ = d.user_store.issue_token("root", "t")
    d._user_token, _ = d.user_store.issue_token("bob", "t")
    return d


def _run(d, script):
    async def go():
        async with TestClient(TestServer(d.app)) as client:
            return await script(client)
    return asyncio.run(go())


def _h(token):
    return {"Authorization": f"Bearer {token}"}


class TestRoutes:
    def test_admin_only(self, tmp_path):
        d = _dashboard(tmp_path)

        async def script(c):
            out = []
            for method, path in (("GET", "/api/settings"), ("PUT", "/api/settings"),
                                 ("POST", "/api/settings/rhythm-test")):
                r = await c.request(method, path, headers=_h(d._user_token), json={})
                out.append(r.status)
            page = await c.get("/settings", headers=_h(d._user_token), allow_redirects=False)
            out.append(page.status)
            return out
        assert _run(d, script) == [403, 403, 403, 302]

    def test_get_put_roundtrip_and_audit_without_secret(self, tmp_path, caplog):
        d = _dashboard(tmp_path)

        async def script(c):
            snap = await (await c.get("/api/settings", headers=_h(d._admin_token))).json()
            r = await c.put("/api/settings", headers=_h(d._admin_token), json={
                "version": snap["version"],
                "changes": {"storage.remote_storage.rhythm.auth_token": "new-token-value",
                            "storage.max_storage_gb": 50}})
            bad = await c.put("/api/settings", headers=_h(d._admin_token), json={
                "version": (await r.json())["version"],
                "changes": {"defacing.device": "tpu"}})
            stale = await c.put("/api/settings", headers=_h(d._admin_token), json={
                "version": snap["version"], "changes": {"defacing.enabled": True}})
            page = await c.get("/settings", headers=_h(d._admin_token))
            return snap, r.status, await r.json(), bad.status, await bad.json(), \
                stale.status, page.status
        with caplog.at_level(logging.INFO):
            snap, st, body, bad_st, bad, stale_st, page_st = _run(d, script)
        assert TOKEN not in repr(snap)
        assert st == 200 and body["applied_live"] == ["storage.max_storage_gb"]
        assert body["restart_required"] == ["storage.remote_storage.rhythm.auth_token"]
        assert d.settings.storage["max_storage_gb"] == 50
        assert bad_st == 400 and "defacing.device" in bad["errors"]
        assert stale_st == 409
        assert page_st == 200
        assert "action=settings_change" in caplog.text
        assert "new-token-value" not in caplog.text and TOKEN not in caplog.text

    def test_cookie_put_needs_csrf_header(self, tmp_path):
        d = _dashboard(tmp_path)

        async def script(c):
            await c.post("/login", json={"username": "root", "password": "root-password"},
                         headers=CSRF)
            r = await c.put("/api/settings", json={"changes": {}, "version": "x"})
            return r.status
        assert _run(d, script) == 403

    def test_example_file_not_editable(self, tmp_path):
        settings = Settings(storage={"base_path": str(tmp_path / "d"),
                                     "temp_path": str(tmp_path / "t")},
                            logging={"anontrail": str(tmp_path / "a.jsonl")})
        d = Dashboard(settings, StorageManager(settings))
        d.user_store.add("root", "root-password", admin=True)
        tok, _ = d.user_store.issue_token("root", "t")

        async def script(c):
            return (await c.get("/api/settings", headers=_h(tok))).status
        assert _run(d, script) == 409


# ------------------------------------------------------------------ probe
class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_session(status=200, body=None, exc=None, seen=None):
    class _Session:
        def __init__(self, headers=None, timeout=None):
            if seen is not None:
                seen["headers"] = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url):
            if seen is not None:
                seen["url"] = url
            if exc:
                raise exc
            return _Resp(status, body)
    return _Session


class TestProbe:
    def test_ok_reports_catalog_uuids(self, monkeypatch):
        seen = {}
        body = {"results": [{"id": "AAAA"}, {"id": "bbbb"}], "next": None}
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession",
                            _fake_session(200, body, seen=seen))
        r = asyncio.run(rhythm_storage.probe_connection(
            "https://r.example/", "tok", ["aaaa", "cccc"]))
        assert r["ok"] and r["protocols"] == 2
        assert r["found"] == ["aaaa"] and r["missing"] == ["cccc"] and not r["partial"]
        assert seen["url"] == "https://r.example/api/v1/protocols-api/?page_size=100"
        assert seen["headers"]["Authorization"] == "Bearer tok"

    def test_rejected_token(self, monkeypatch):
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake_session(401, {}))
        r = asyncio.run(rhythm_storage.probe_connection("https://r.example", "tok"))
        assert not r["ok"] and r["status"] == 401 and "rejected" in r["message"]

    def test_network_error(self, monkeypatch):
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession",
                            _fake_session(exc=OSError("unreachable")))
        r = asyncio.run(rhythm_storage.probe_connection("https://r.example", "tok"))
        assert not r["ok"] and r["status"] is None and "Cannot reach" in r["message"]

    def test_route_falls_back_to_saved_credentials(self, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession",
                            _fake_session(200, [], seen=seen))
        d = _dashboard(tmp_path)

        async def script(c):
            r = await c.post("/api/settings/rhythm-test", headers=_h(d._admin_token), json={})
            return await r.json()
        r = _run(d, script)
        assert r["ok"]
        assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert seen["url"].startswith("https://r.example/")
