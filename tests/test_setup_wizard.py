"""First-run setup wizard: the code gate while no user exists, admin gate
after, validate-everything-before-writing, settings.yaml created from the
example, the marker that retires /setup, and the upgrade path."""
import asyncio
import logging
import shutil
import stat
from pathlib import Path

import yaml
from aiohttp.test_utils import TestClient, TestServer

from pixieveil.config import Settings
from pixieveil.dashboard import setup as setup_mod
from pixieveil.dashboard.server import COOKIE_NAME, Dashboard
from pixieveil.storage import rhythm_storage
from pixieveil.storage.storage_manager import StorageManager

REPO_EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "settings.example.yaml"
CSRF = {"X-Requested-With": "PixieVeil"}
TOKEN = "rhythm-secret-token"


def _fresh(tmp_path, with_settings=False) -> Dashboard:
    """A server started on the example file (no settings.yaml), no users."""
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    example = cfg / "settings.example.yaml"
    doc = yaml.safe_load(REPO_EXAMPLE.read_text())
    doc["storage"].update(base_path=str(tmp_path / "data"), temp_path=str(tmp_path / "tmp"),
                          export_path=str(tmp_path / "export"))
    doc["logging"] = {"anontrail": str(tmp_path / "anontrail.jsonl")}
    doc["http_server"]["users_file"] = str(cfg / "users.yaml")
    doc["defacing"]["enabled"] = False
    example.write_text(yaml.safe_dump(doc, sort_keys=False))
    source = example
    if with_settings:
        source = cfg / "settings.yaml"
        shutil.copy(example, source)
    settings = Settings.load(source)
    return Dashboard(settings, StorageManager(settings))


def _run(d, script):
    async def go():
        async with TestClient(TestServer(d.app)) as client:
            return await script(client)
    return asyncio.run(go())


def _code(d) -> str:
    return d.setup.announce_code()


def _h(d, code=None):
    return {**CSRF, "X-Setup-Code": code if code is not None else _code(d)}


GOOD_CHANGES = {
    "dicom_server.ae_title": "HOSPITALPV",
    "dicom_server.port": 11112,
    "anonymization.profile": "rhythm",
    "storage.remote_storage.rhythm.base_url": "https://rhythm.example/",
    "storage.remote_storage.rhythm.auth_token": TOKEN,
    "storage.remote_storage.rhythm.site_code": "S014",
    "defacing.enabled": True,
    "defacing.device": "cpu",
    "defacing.keep_backup": False,
    "storage.retain_after_export_days": 14,
    "storage.max_storage_gb": 200,
}
GOOD_ADMIN = {"username": "Alice", "password": "alice-password"}


class TestNoUsers:
    def test_everything_leads_to_setup(self, tmp_path):
        d = _fresh(tmp_path)

        async def script(c):
            html = {"Accept": "text/html"}
            r1 = await c.get("/", headers=html, allow_redirects=False)
            r2 = await c.get("/login", headers=html, allow_redirects=False)
            r3 = await c.get("/api/studies")
            r4 = await c.get("/setup", headers=html)
            r5 = await c.get("/api/setup")
            return (r1.status, r1.headers.get("Location"), r2.headers.get("Location"),
                    r3.status, await r3.json(), r4.status, await r5.json())
        s1, loc1, loc2, s3, b3, s4, b5 = _run(d, script)
        assert (s1, loc1, loc2) == (302, "/setup", "/setup")
        assert s3 == 401 and b3["setup_required"] is True
        assert s4 == 200
        assert b5["needs_code"] and b5["needs_admin"] and "fields" not in b5

    def test_code_file_written_owner_only(self, tmp_path):
        d = _fresh(tmp_path)
        code = _code(d)
        f = tmp_path / "config" / ".setup_code"
        assert f.read_text().strip() == code
        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_wrong_code_and_lockout(self, tmp_path):
        d = _fresh(tmp_path)

        async def script(c):
            out = []
            for _ in range(6):
                r = await c.post("/api/setup/verify-code", headers=_h(d, "AAAA-AAAA"), json={})
                out.append(r.status)
            right = await c.post("/api/setup/verify-code", headers=_h(d), json={})
            return out, right.status
        statuses, right = _run(d, script)
        assert statuses == [403] * 5 + [429]
        assert right == 429                     # locked even with the right code

    def test_code_accepted_without_dash_and_lowercase(self, tmp_path):
        d = _fresh(tmp_path)
        code = _code(d).replace("-", "").lower()

        async def script(c):
            r = await c.post("/api/setup/verify-code", headers=_h(d, code), json={})
            st = await (await c.get("/api/setup", headers=_h(d, code))).json()
            return r.status, st
        status, st = _run(d, script)
        assert status == 200
        fields = {f["path"]: f for f in st["fields"]}
        # Fresh install: the wizard's recommendations, not the example's values.
        assert fields["anonymization.profile"]["value"] == "rhythm"
        assert fields["defacing.keep_backup"]["value"] is False
        assert "defacing" in st

    def test_finish_needs_code_and_csrf(self, tmp_path):
        d = _fresh(tmp_path)

        async def script(c):
            no_code = await c.post("/api/setup/finish", headers=CSRF,
                                   json={"admin": GOOD_ADMIN, "changes": {}})
            no_csrf = await c.post("/api/setup/finish", headers={"X-Setup-Code": _code(d)},
                                   json={"admin": GOOD_ADMIN, "changes": {}})
            return no_code.status, no_csrf.status
        assert _run(d, script) == (403, 403)
        assert d.user_store.count() == 0

    def test_full_finish(self, tmp_path, caplog):
        d = _fresh(tmp_path)

        async def script(c):
            r = await c.post("/api/setup/finish", headers=_h(d),
                             json={"admin": GOOD_ADMIN, "changes": GOOD_CHANGES})
            body = await r.json()
            cookie = r.cookies.get(COOKIE_NAME)
            me = await c.get("/api/me")
            after = await c.get("/setup", allow_redirects=False)
            api_after = await c.get("/api/setup")
            return r.status, body, cookie, me.status, await me.json(), after.status, \
                api_after.status
        with caplog.at_level(logging.INFO):
            status, body, cookie, me_st, me, after, api_after = _run(d, script)
        assert status == 200 and body["username"] == "alice"
        assert cookie is not None and me_st == 200 and me["admin"] is True
        assert after == 404 and api_after == 404

        path = tmp_path / "config" / "settings.yaml"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        doc = yaml.safe_load(path.read_text())
        assert doc["dicom_server"]["ae_title"] == "HOSPITALPV"
        assert doc["dicom_server"]["port"] == 11112
        assert doc["anonymization"]["profile"] == "rhythm"
        assert doc["anonymization"]["profiles"]          # untouched rest of the file
        rh = doc["storage"]["remote_storage"]["rhythm"]
        assert rh == {**rh, "base_url": "https://rhythm.example", "auth_token": TOKEN,
                      "site_code": "S014"}
        assert doc["defacing"]["enabled"] is True and doc["defacing"]["keep_backup"] is False
        assert doc["storage"]["retain_after_export_days"] == 14
        assert "dicom_server.port" in body["restart_required"]

        cfg = tmp_path / "config"
        assert (cfg / ".setup_complete").exists()
        assert not (cfg / ".setup_code").exists()
        assert TOKEN not in caplog.text and "alice-password" not in caplog.text
        # /settings works now and still keeps the port read-only.
        assert d.settings_editor is not None and d.settings_editor.path == path

    def test_invalid_input_writes_nothing(self, tmp_path):
        d = _fresh(tmp_path)

        async def script(c):
            r = await c.post("/api/setup/finish", headers=_h(d), json={
                "admin": {"username": "x", "password": "short"},
                "changes": {**GOOD_CHANGES, "dicom_server.port": 70000,
                            "dicom_server.ae_title": "BAD\\AE",
                            "http_server.port": 1}})
            return r.status, await r.json()
        status, body = _run(d, script)
        assert status == 400
        assert set(body["errors"]) >= {"admin.username", "admin.password", "dicom_server.port",
                                       "dicom_server.ae_title", "http_server.port"}
        assert d.user_store.count() == 0
        assert not (tmp_path / "config" / "settings.yaml").exists()
        assert not (tmp_path / "config" / ".setup_complete").exists()

    def test_skip_refused_without_admin(self, tmp_path):
        d = _fresh(tmp_path)

        async def script(c):
            return (await c.post("/api/setup/skip", headers=_h(d), json={})).status
        assert _run(d, script) == 409

    def test_status_hides_saved_token(self, tmp_path):
        d = _fresh(tmp_path, with_settings=True)
        path = tmp_path / "config" / "settings.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["storage"].setdefault("remote_storage", {})["rhythm"] = {"auth_token": TOKEN}
        path.write_text(yaml.safe_dump(doc))

        async def script(c):
            return await (await c.get("/api/setup", headers=_h(d))).text()
        text = _run(d, script)
        assert TOKEN not in text and '"is_set": true' in text

    def test_rhythm_test_uses_probe(self, tmp_path, monkeypatch):
        d = _fresh(tmp_path)
        seen = {}

        async def fake_probe(base_url, token, uuids=(), timeout_s=10.0):
            seen.update(base_url=base_url, token=token)
            return {"ok": True, "status": 200, "message": "Connected"}
        monkeypatch.setattr(rhythm_storage, "probe_connection", fake_probe)

        async def script(c):
            r = await c.post("/api/setup/rhythm-test", headers=_h(d), json={
                "base_url": "https://r.example/", "auth_token": "t"})
            return await r.json()
        assert _run(d, script)["ok"]
        assert seen == {"base_url": "https://r.example", "token": "t"}


class TestUsersExist:
    def _dash(self, tmp_path):
        d = _fresh(tmp_path, with_settings=True)
        d.user_store.add("root", "root-password", admin=True)
        d.user_store.add("bob", "bob-password")
        return d

    async def _login(self, c, user, pw):
        await c.post("/login", json={"username": user, "password": pw}, headers=CSRF)

    def test_admin_redirected_once_user_not(self, tmp_path):
        d = self._dash(tmp_path)
        html = {"Accept": "text/html"}

        async def script(c):
            await self._login(c, "bob", "bob-password")
            bob = await c.get("/", headers=html, allow_redirects=False)
            bob_setup = await c.get("/setup", headers=html, allow_redirects=False)
            await self._login(c, "root", "root-password")
            root = await c.get("/", headers=html, allow_redirects=False)
            about = await c.get("/about", headers=html, allow_redirects=False)
            st = await (await c.get("/api/setup")).json()
            skip = await c.post("/api/setup/skip", headers=CSRF, json={})
            again = await c.get("/", headers=html, allow_redirects=False)
            return (bob.status, bob_setup.headers.get("Location"), root.status,
                    root.headers.get("Location"), about.status, st, skip.status, again.status)
        bob, bob_setup, root, root_loc, about, st, skip, again = _run(d, script)
        assert bob == 200 and bob_setup == "/"
        assert (root, root_loc) == (302, "/setup")
        assert about == 200
        assert st["needs_code"] is False and st["user"] == "root" and "fields" in st
        assert skip == 200 and again == 200

    def test_finish_as_admin_without_code(self, tmp_path):
        d = self._dash(tmp_path)

        async def script(c):
            await self._login(c, "root", "root-password")
            r = await c.post("/api/setup/finish", headers=CSRF,
                             json={"changes": {"dicom_server.ae_title": "NEWAE"}})
            return r.status, await r.json()
        status, body = _run(d, script)
        assert status == 200 and body["username"] == "root"
        doc = yaml.safe_load((tmp_path / "config" / "settings.yaml").read_text())
        assert doc["dicom_server"]["ae_title"] == "NEWAE"
        assert d.user_store.count() == 2

    def test_token_user_not_redirected(self, tmp_path):
        d = self._dash(tmp_path)
        tok, _ = d.user_store.issue_token("root", "t")

        async def script(c):
            r = await c.get("/", headers={"Accept": "text/html",
                                          "Authorization": f"Bearer {tok}"},
                            allow_redirects=False)
            return r.status
        assert _run(d, script) == 200

    def test_existing_deployment_marked_complete_at_startup(self, tmp_path):
        d = self._dash(tmp_path)
        d.setup.startup()
        assert not d.setup.required
        assert (tmp_path / "config" / ".setup_complete").exists()
        assert not (tmp_path / "config" / ".setup_code").exists()

    def test_startup_without_users_announces_code(self, tmp_path, caplog):
        d = _fresh(tmp_path)
        with caplog.at_level(logging.WARNING):
            d.setup.startup()
        assert d.setup.required
        assert d.setup.announce_code() in caplog.text


def test_settings_page_still_refuses_setup_only_fields(tmp_path):
    d = _fresh(tmp_path, with_settings=True)
    d.user_store.add("root", "root-password", admin=True)
    d.setup.complete("test")
    tok, _ = d.user_store.issue_token("root", "t")

    async def script(c):
        h = {"Authorization": f"Bearer {tok}"}
        snap = await (await c.get("/api/settings", headers=h)).json()
        r = await c.put("/api/settings", headers=h, json={
            "version": snap["version"], "changes": {"dicom_server.port": 5000}})
        return r.status, await r.json()
    status, body = _run(d, script)
    assert status == 400 and "dicom_server.port" in body["errors"]


def test_settings_target():
    assert setup_mod.settings_target(Path("c/settings.example.yaml")) == Path("c/settings.yaml")
    assert setup_mod.settings_target(Path("c/other.yaml")) == Path("c/other.yaml")
    assert setup_mod.settings_target(None) is None
