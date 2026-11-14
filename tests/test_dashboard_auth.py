"""Dashboard login, sessions, CSRF guard, admin user management, attribution.

Same in-process aiohttp TestServer/TestClient harness as test_dashboard.py.
Each test drives one client (one cookie jar) through a scripted sequence.
"""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer

from pixieveil.auth.sessions import COOKIE_NAME
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.study_sidecar import StudySidecar

from .conftest import make_settings
from .test_dashboard import _complete_exam, auth_headers, make_dashboard
from .test_storage_manager import register_study

CSRF = {"X-Requested-With": "PixieVeil"}


def _dash(tmp_path, **http_server):
    d = make_dashboard(make_settings(tmp_path, http_server=http_server))
    d.user_store.add("alice", "alice-password")
    return d


def _run(dashboard, script):
    """script(client) is an async function; runs it on a fresh client."""
    async def go():
        async with TestClient(TestServer(dashboard.app)) as client:
            return await script(client)
    return asyncio.run(go())


async def _login(client, username="alice", password="alice-password", **extra):
    return await client.post("/login", json={"username": username, "password": password,
                                             **extra}, headers=CSRF)


async def _body(resp):
    text = await resp.text()
    try:
        return json.loads(text)
    except ValueError:
        return text


class TestUnauthenticated:
    def test_page_redirects_to_login_with_next(self, tmp_path):
        async def script(client):
            r = await client.get("/studies?x=1", headers={"Accept": "text/html"},
                                 allow_redirects=False)
            return r.status, r.headers["Location"]
        status, loc = _run(_dash(tmp_path), script)
        assert status == 302
        url = urlsplit(loc)
        assert url.path == "/login"
        assert parse_qs(url.query)["next"] == ["/studies?x=1"]

    def test_api_and_sse_get_401(self, tmp_path):
        async def script(client):
            return [(await client.get(p)).status
                    for p in ("/api/studies", "/stats", "/api/events", "/api/me")]
        assert _run(_dash(tmp_path), script) == [401] * 4

    def test_public_paths(self, tmp_path):
        async def script(client):
            return [(await client.get(p)).status for p in
                    ("/login", "/health", "/static/theme.css", "/static/common.js")]
        assert _run(_dash(tmp_path), script) == [200] * 4

    def test_other_static_assets_are_not_public(self, tmp_path):
        async def script(client):
            return (await client.get("/static/nope.js")).status
        assert _run(_dash(tmp_path), script) == 401


class TestLogin:
    def test_login_sets_httponly_strict_cookie_and_grants_access(self, tmp_path):
        async def script(client):
            r = await _login(client, next="/studies")
            morsel = r.cookies[COOKIE_NAME]
            me = await client.get("/api/me")
            return r.status, await r.json(), morsel, me.status, await me.json()
        status, body, morsel, me_status, me = _run(_dash(tmp_path), script)
        assert status == 200 and body == {"ok": True, "next": "/studies"}
        assert morsel["httponly"] and morsel["samesite"] == "Strict"
        assert not morsel["secure"]
        assert me_status == 200
        assert me == {"username": "alice", "admin": False, "via": "session"}

    def test_secure_cookie_flag(self, tmp_path):
        async def script(client):
            return (await _login(client)).cookies[COOKIE_NAME]["secure"]
        assert _run(_dash(tmp_path, secure_cookies=True), script)

    def test_wrong_password_is_401_and_sets_nothing(self, tmp_path):
        async def script(client):
            r = await _login(client, password="wrong-password")
            return r.status, COOKIE_NAME in r.cookies, (await client.get("/api/me")).status
        assert _run(_dash(tmp_path), script) == (401, False, 401)

    def test_open_redirect_rejected(self, tmp_path):
        async def script(client):
            return [(await (await _login(client, next=n)).json())["next"]
                    for n in ("//evil.example", "https://evil.example", "/ok", "\\\\evil")]
        assert _run(_dash(tmp_path), script) == ["/", "/", "/ok", "/"]

    def test_login_requires_csrf_header(self, tmp_path):
        async def script(client):
            r = await client.post("/login", json={"username": "alice",
                                                  "password": "alice-password"})
            return r.status
        assert _run(_dash(tmp_path), script) == 403

    def test_lockout_after_repeated_failures(self, tmp_path):
        async def script(client):
            statuses = [(await _login(client, password="bad-password")).status
                        for _ in range(5)]
            # even the right password is refused while locked
            statuses.append((await _login(client)).status)
            return statuses
        assert _run(_dash(tmp_path), script) == [401] * 5 + [429]

    def test_no_users_configured_explains(self, tmp_path):
        from pixieveil.dashboard.server import Dashboard
        from pixieveil.storage.storage_manager import StorageManager
        settings = make_settings(tmp_path)
        d = Dashboard(settings, StorageManager(settings))

        async def script(client):
            r = await _login(client)
            return r.status, await r.json()
        status, body = _run(d, script)
        assert status == 503 and "pixieveil.auth add" in body["error"]

    def test_logged_in_user_visiting_login_is_sent_on(self, tmp_path):
        async def script(client):
            await _login(client)
            r = await client.get("/login?next=/studies", allow_redirects=False)
            return r.status, r.headers.get("Location")
        assert _run(_dash(tmp_path), script) == (302, "/studies")


class TestSessionLifecycle:
    def test_logout_clears_cookie(self, tmp_path):
        async def script(client):
            await _login(client)
            r = await client.post("/logout", headers=CSRF)
            return r.status, (await client.get("/api/me")).status
        assert _run(_dash(tmp_path), script) == (200, 401)

    def test_disable_kills_existing_session(self, tmp_path):
        d = _dash(tmp_path)

        async def script(client):
            await _login(client)
            before = (await client.get("/api/me")).status
            d.user_store.set_disabled("alice", True)
            return before, (await client.get("/api/me")).status
        assert _run(d, script) == (200, 401)

    def test_own_password_change_keeps_this_session(self, tmp_path):
        d = _dash(tmp_path)

        async def script(client):
            await _login(client)
            r = await client.post("/api/me/password", headers=CSRF,
                                  json={"current": "alice-password", "new": "new-password"})
            return r.status, (await client.get("/api/me")).status
        assert _run(d, script) == (200, 200)
        assert d.user_store.authenticate("alice", "new-password") is not None


class TestCsrf:
    def test_session_mutation_without_header_is_403(self, tmp_path):
        async def script(client):
            await _login(client)
            r = await client.put("/api/studies/1/exam", json={"fields": {"bogus": 1}})
            return r.status
        assert _run(_dash(tmp_path), script) == 403

    def test_cross_origin_is_403(self, tmp_path):
        async def script(client):
            await _login(client)
            r = await client.put("/api/studies/1/exam", json={"fields": {"bogus": 1}},
                                 headers={**CSRF, "Origin": "http://evil.example"})
            return r.status
        assert _run(_dash(tmp_path), script) == 403

    def test_with_header_passes_middleware(self, tmp_path):
        async def script(client):
            await _login(client)
            r = await client.put("/api/studies/1/exam", json={"fields": {"bogus": 1}},
                                 headers=CSRF)
            return r.status
        assert _run(_dash(tmp_path), script) == 400   # validation, i.e. past auth


class TestApiTokensEndpoints:
    def test_issue_use_and_revoke_own_token(self, tmp_path):
        d = _dash(tmp_path)

        async def script(client):
            await _login(client)
            issued = await (await client.post("/api/me/tokens", headers=CSRF,
                                              json={"name": "watcher"})).json()
            listed = await (await client.get("/api/me/tokens")).json()
            async with TestClient(TestServer(d.app)) as other:
                hdr = {"Authorization": f"Bearer {issued['token']}"}
                used = await (await other.get("/api/me", headers=hdr)).json()
                await client.delete(f"/api/me/tokens/{issued['meta']['id']}", headers=CSRF)
                after = (await other.get("/api/me", headers=hdr)).status
            return issued, listed, used, after
        issued, listed, used, after = _run(d, script)
        assert [t["name"] for t in listed["tokens"]] == ["watcher"]
        assert "hash" not in listed["tokens"][0]
        assert used == {"username": "alice", "admin": False, "via": "token"}
        assert after == 401


class TestAdmin:
    def test_non_admin_is_forbidden(self, tmp_path):
        async def script(client):
            await _login(client)
            page = await client.get("/users", allow_redirects=False)
            return (page.status, (await client.get("/api/users")).status,
                    (await client.post("/api/users", headers=CSRF,
                                       json={"username": "eve", "password": "password1"})).status)
        assert _run(_dash(tmp_path), script) == (302, 403, 403)

    def test_lookup_is_admin_only(self, tmp_path):
        async def script(client):
            await _login(client)
            page = await client.get("/lookup", allow_redirects=False)
            return page.status, (await client.get("/api/lookup?q=pid")).status
        assert _run(_dash(tmp_path), script) == (302, 403)

    def test_admin_manages_users(self, tmp_path):
        d = _dash(tmp_path)
        h = auth_headers(d)

        async def script(client):
            out = []
            out.append((await client.post("/api/users", headers=h, json={
                "username": "bob", "password": "bob-password", "admin": False})).status)
            out.append((await client.post("/api/users", headers=h, json={
                "username": "bob", "password": "bob-password"})).status)   # duplicate
            out.append((await client.patch("/api/users/bob", headers=h,
                                           json={"disabled": True})).status)
            out.append((await client.post("/api/users/bob/password", headers=h,
                                          json={"password": "x"})).status)  # too short
            out.append((await client.delete("/api/users/admin", headers=h)).status)  # self
            out.append((await client.delete("/api/users/ghost", headers=h)).status)
            users = await (await client.get("/api/users", headers=h)).json()
            out.append((await client.delete("/api/users/bob", headers=h)).status)
            return out, users
        statuses, users = _run(d, script)
        assert statuses == [200, 400, 200, 400, 400, 404, 200]
        bob = next(u for u in users["users"] if u["username"] == "bob")
        assert bob["disabled"] is True
        assert "password_hash" not in json.dumps(users)
        assert d.user_store.get("bob") is None

    def test_admin_page_served(self, tmp_path):
        d = _dash(tmp_path)

        async def script(client):
            r = await client.get("/users", headers=auth_headers(d))
            return r.status, await r.text()
        status, body = _run(d, script)
        assert status == 200 and "/api/users" in body and "cdn." not in body


class TestAttribution:
    def test_actions_and_edits_are_attributed(self, tmp_path):
        d = _dash(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        exam = _complete_exam(1)
        exam["image_quality"] = None
        ExamSidecar(1, exam).save(sm.base_path)

        async def script(client):
            await _login(client)
            await client.put("/api/studies/1/exam", headers=CSRF,
                             json={"fields": {"image_quality": "Diagnostic"}})
            await client.post("/api/studies/1/actions/mark_exported", headers=CSRF,
                              json={"via": "rhythm"})
            return await (await client.get("/api/studies/1")).json()
        detail = _run(d, script)

        sc = StudySidecar.load(StudySidecar.path_for(sm.base_path, 1))
        assert sc.exported_by == "alice"
        assert [(a["user"], a["action"], a["ok"]) for a in sc.audit] == [
            ("alice", "save_exam", True), ("alice", "mark_exported", True)]
        assert ExamSidecar.load(ExamSidecar.path_for(sm.base_path, 1)).data["manual"][
            "edited_by"] == "alice"
        assert detail["study"]["exported_by"] == "alice"
        assert len(detail["study"]["audit"]) == 2

    def test_failed_action_is_recorded_without_exported_by(self, tmp_path):
        d = _dash(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")

        async def script(client):
            return (await client.post("/api/studies/1/actions/send_dicom",
                                      headers=auth_headers(d))).status
        assert _run(d, script) == 400       # DICOM export not configured
        sc = StudySidecar.load(StudySidecar.path_for(sm.base_path, 1))
        assert sc.exported_by is None
        assert sc.audit[-1]["user"] == "admin" and sc.audit[-1]["ok"] is False
