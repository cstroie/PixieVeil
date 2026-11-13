"""Obtaining the RHYTHM API token from /settings: RHYTHM's login + 2FA flow
driven server-side (against a scripted fake aiohttp session — no network),
the token saved straight into settings.yaml and never returned or logged,
and the bookmarklet helper present on the page."""
import asyncio
import logging

import yaml
from aiohttp.test_utils import TestClient, TestServer

from pixieveil.dashboard.server import Dashboard
from pixieveil.storage import rhythm_storage
from pixieveil.storage.storage_manager import StorageManager

from .test_settings_editor import _write_settings

NEW_TOKEN = "fresh-rhythm-token-0123456789"
CSRF = {"X-Requested-With": "PixieVeil"}


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake(script: dict, calls: list, exc=None):
    """script: path -> list of (status, body) returned in order."""
    class _Session:
        def __init__(self, cookie_jar=None, timeout=None, headers=None):
            self.jar = cookie_jar

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None):
            path = url.split("://", 1)[1].split("/", 1)[1]
            calls.append(("/" + path, json, id(self.jar)))
            if exc:
                raise exc
            return _Resp(*script["/" + path].pop(0))
    return _Session


LOGIN = rhythm_storage.LOGIN_PATH
VERIFY = rhythm_storage.VERIFY_2FA_PATH


class TestHelpers:
    def test_token_without_2fa(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(
            {LOGIN: [(200, {"token": "t1", "username": "u"})]}, calls))
        r = asyncio.run(rhythm_storage.rhythm_login("https://r.example/", "u", "pw"))
        assert r["ok"] and r["token"] == "t1" and r["username"] == "u"
        assert calls[0][:2] == (LOGIN, {"username": "u", "password": "pw"})

    def test_2fa_reuses_the_cookie_jar(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(
            {LOGIN: [(200, {"requires_2fa": True})],
             VERIFY: [(401, {"error": "Invalid code. Please try again."}),
                      (200, {"token": "t2", "username": "u"})]}, calls))

        async def go():
            first = await rhythm_storage.rhythm_login("https://r.example", "u", "pw")
            bad = await rhythm_storage.rhythm_verify_2fa("https://r.example", first["jar"], "1")
            good = await rhythm_storage.rhythm_verify_2fa("https://r.example", first["jar"],
                                                          "123456")
            return first, bad, good
        first, bad, good = asyncio.run(go())
        assert first["needs_2fa"] and not first["ok"]
        assert not bad["ok"] and "Invalid code" in bad["message"]
        assert good["ok"] and good["token"] == "t2"
        assert len({c[2] for c in calls}) == 1          # one jar → one Django session
        assert calls[-1][1] == {"code": "123456"}

    def test_2fa_not_enrolled_and_wrong_password_and_network(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(
            {LOGIN: [(200, {"requires_2fa_setup": True}),
                     (401, {"error": "Invalid credentials"})]}, calls))
        setup = asyncio.run(rhythm_storage.rhythm_login("https://r.example", "u", "pw"))
        wrong = asyncio.run(rhythm_storage.rhythm_login("https://r.example", "u", "bad"))
        assert not setup["ok"] and "enrol" in setup["message"]
        assert not wrong["ok"] and wrong["message"] == "Invalid credentials"
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession",
                            _fake({}, [], exc=OSError("down")))
        net = asyncio.run(rhythm_storage.rhythm_login("https://r.example", "u", "pw"))
        assert not net["ok"] and "Cannot reach" in net["message"]


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


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _saved(tmp_path):
    doc = yaml.safe_load((tmp_path / "config" / "settings.yaml").read_text())
    return doc["storage"]["remote_storage"]["rhythm"]


class TestRoutes:
    BODY = {"base_url": "https://rhythm.example/", "username": "costin", "password": "pw"}

    def test_admin_only_and_csrf(self, tmp_path):
        d = _dashboard(tmp_path)

        async def script(c):
            a = await c.post("/api/settings/rhythm-login", headers=_h(d._user_token),
                             json=self.BODY)
            b = await c.post("/api/settings/rhythm-login/verify", headers=_h(d._user_token),
                             json={"pending": "x", "code": "1"})
            await c.post("/login", json={"username": "root", "password": "root-password"},
                         headers=CSRF)
            no_csrf = await c.post("/api/settings/rhythm-login", json=self.BODY)
            return a.status, b.status, no_csrf.status
        assert _run(d, script) == (403, 403, 403)

    def test_2fa_flow_saves_token_never_returns_it(self, tmp_path, monkeypatch, caplog):
        calls = []
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(
            {LOGIN: [(200, {"requires_2fa": True})],
             VERIFY: [(401, {"error": "Invalid code. Please try again."}),
                      (200, {"token": NEW_TOKEN, "username": "costin"})]}, calls))
        d = _dashboard(tmp_path)

        async def script(c):
            r1 = await c.post("/api/settings/rhythm-login", headers=_h(d._admin_token),
                              json=self.BODY)
            b1 = await r1.text()
            pending = (await r1.json())["pending"]
            r2 = await c.post("/api/settings/rhythm-login/verify", headers=_h(d._admin_token),
                              json={"pending": pending, "code": "000000"})
            b2 = await r2.text()
            r3 = await c.post("/api/settings/rhythm-login/verify", headers=_h(d._admin_token),
                              json={"pending": pending, "code": "123 456"})
            b3 = await r3.text()
            reuse = await c.post("/api/settings/rhythm-login/verify",
                                 headers=_h(d._admin_token),
                                 json={"pending": pending, "code": "123456"})
            return b1, b2, r3.status, b3, reuse.status
        with caplog.at_level(logging.INFO):
            b1, b2, s3, b3, reuse = _run(d, script)
        assert '"needs_2fa": true' in b1 and "pw" not in b1
        assert "Invalid code" in b2
        assert s3 == 200 and '"ok": true' in b3 and '"username": "costin"' in b3
        assert NEW_TOKEN not in b1 + b2 + b3
        assert NEW_TOKEN not in caplog.text and "action=rhythm_token_obtained" in caplog.text
        assert calls[-1][1] == {"code": "123456"}
        rh = _saved(tmp_path)
        assert rh["auth_token"] == NEW_TOKEN and rh["base_url"] == "https://rhythm.example"
        assert reuse == 400                       # pending used up

    def test_direct_token_saved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(
            {LOGIN: [(200, {"token": NEW_TOKEN, "username": "costin"})]}, []))
        d = _dashboard(tmp_path)

        async def script(c):
            r = await c.post("/api/settings/rhythm-login", headers=_h(d._admin_token),
                             json=self.BODY)
            return r.status, await r.json()
        status, body = _run(d, script)
        assert status == 200 and body["ok"]
        assert "storage.remote_storage.rhythm.auth_token" in body["restart_required"]
        assert _saved(tmp_path)["auth_token"] == NEW_TOKEN

    def test_pending_belongs_to_its_admin_and_expires(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(
            {LOGIN: [(200, {"requires_2fa": True})] * 2}, []))
        d = _dashboard(tmp_path)
        d.user_store.add("other", "other-password", admin=True)
        other_tok, _ = d.user_store.issue_token("other", "t")

        async def script(c):
            r = await c.post("/api/settings/rhythm-login", headers=_h(d._admin_token),
                             json=self.BODY)
            pending = (await r.json())["pending"]
            stolen = await c.post("/api/settings/rhythm-login/verify", headers=_h(other_tok),
                                  json={"pending": pending, "code": "123456"})
            # Another admin's attempt is refused (and fails closed: the
            # sign-in is dropped), so start a fresh one for the expiry case.
            r = await c.post("/api/settings/rhythm-login", headers=_h(d._admin_token),
                             json=self.BODY)
            pending = (await r.json())["pending"]
            admin, base, jar, _exp = d._rhythm_logins[pending]
            d._rhythm_logins[pending] = (admin, base, jar, 0.0)
            expired = await c.post("/api/settings/rhythm-login/verify",
                                   headers=_h(d._admin_token),
                                   json={"pending": pending, "code": "123456"})
            unknown = await c.post("/api/settings/rhythm-login/verify",
                                   headers=_h(d._admin_token),
                                   json={"pending": "nope", "code": "1"})
            return stolen.status, expired.status, unknown.status
        assert _run(d, script) == (400, 400, 400)
        assert _saved(tmp_path)["auth_token"] != NEW_TOKEN

    def test_bad_input_rejected_before_any_request(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake({}, calls))
        d = _dashboard(tmp_path)

        async def script(c):
            a = await c.post("/api/settings/rhythm-login", headers=_h(d._admin_token),
                             json={**self.BODY, "base_url": "ftp://x"})
            b = await c.post("/api/settings/rhythm-login", headers=_h(d._admin_token),
                             json={**self.BODY, "password": ""})
            return a.status, b.status
        assert _run(d, script) == (400, 400)
        assert calls == []


def test_settings_page_has_token_helpers(tmp_path):
    d = _dashboard(tmp_path)

    async def script(c):
        return await (await c.get("/settings", headers=_h(d._admin_token))).text()
    html = _run(d, script)
    assert "function rhythmTokenHelper()" in html
    assert 'id="bookmarklet"' in html and 'id="rhythm-signin"' in html
    assert "/api/v1/scanner-profiles/" in html
