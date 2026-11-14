"""The four documented ways to add dashboard users (USERS.md), end to end:
installer prompt, CLI, the admin /users page, and hand-editing users.yaml —
plus login, sessions and API tokens exercised over HTTP for users created
each way.

Uses the same in-process aiohttp TestServer/TestClient harness as
test_dashboard_auth.py; no network, no real config.
"""

import asyncio
import importlib.util
import io
import os
import time
from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from pixieveil.auth import cli
from pixieveil.auth.sessions import COOKIE_NAME, REFRESH_AFTER_S
from pixieveil.auth.user_store import UserStore
from pixieveil.dashboard.server import Dashboard
from pixieveil.storage.storage_manager import StorageManager
from pixieveil.storage.study_sidecar import StudySidecar

from .conftest import make_settings
from .test_storage_manager import register_study

CSRF = {"X-Requested-With": "PixieVeil"}
REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers

def _users_file(tmp_path) -> Path:
    return tmp_path / "auth" / "users.yaml"


def _dashboard(tmp_path) -> Dashboard:
    settings = make_settings(tmp_path, http_server={"users_file": str(_users_file(tmp_path))})
    return Dashboard(settings, StorageManager(settings))


def _run(dashboard, script):
    async def go():
        async with TestClient(TestServer(dashboard.app)) as client:
            return await script(client)
    return asyncio.run(go())


async def _login(client, username, password):
    return await client.post("/login", headers=CSRF,
                             json={"username": username, "password": password})


async def _can_login(client, username, password) -> bool:
    r = await _login(client, username, password)
    if r.status != 200:
        return False
    return (await client.get("/api/me")).status == 200


def _bump_mtime(path: Path) -> None:
    """Make an external edit visible to a store that caches by mtime."""
    t = time.time() + 5
    os.utime(path, (t, t))


def _cli(tmp_path, *argv, stdin=None, monkeypatch=None) -> int:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(["--file", str(_users_file(tmp_path)), *argv])


def _load_install_module():
    spec = importlib.util.spec_from_file_location("pv_install", REPO / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 1. installer

class TestInstallerPrompt:
    @pytest.fixture
    def install(self):
        return _load_install_module()

    def _settings(self, tmp_path):
        return make_settings(tmp_path, http_server={"users_file": str(_users_file(tmp_path))})

    def test_interactive_creates_first_admin(self, tmp_path, monkeypatch, install):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "Root")
        monkeypatch.setattr("getpass.getpass", lambda _prompt: "root-password")

        assert install.ensure_dashboard_admin(self._settings(tmp_path), False) is True

        user = UserStore(_users_file(tmp_path)).authenticate("root", "root-password")
        assert user is not None and user.admin

        d = _dashboard(tmp_path)
        assert _run(d, lambda c: _can_login(c, "root", "root-password"))

    def test_retries_on_mismatch_then_succeeds(self, tmp_path, monkeypatch, install):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "root")
        answers = iter(["root-password", "typo-password", "root-password", "root-password"])
        monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

        assert install.ensure_dashboard_admin(self._settings(tmp_path), False) is True
        assert UserStore(_users_file(tmp_path)).get("root") is not None

    def test_short_password_retried(self, tmp_path, monkeypatch, install):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "root")
        answers = iter(["short", "short", "long-enough-1", "long-enough-1"])
        monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

        assert install.ensure_dashboard_admin(self._settings(tmp_path), False) is True
        assert UserStore(_users_file(tmp_path)).authenticate("root", "long-enough-1")

    def test_blank_username_skips(self, tmp_path, monkeypatch, install):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "")
        assert install.ensure_dashboard_admin(self._settings(tmp_path), False) is True
        assert UserStore(_users_file(tmp_path)).count() == 0

    def test_non_interactive_only_prints_hint(self, tmp_path, monkeypatch, install, capsys):
        def boom(_prompt):
            raise AssertionError("must not prompt")
        monkeypatch.setattr("builtins.input", boom)
        assert install.ensure_dashboard_admin(self._settings(tmp_path), True) is True
        assert "pixieveil.auth add" in capsys.readouterr().out
        assert UserStore(_users_file(tmp_path)).count() == 0

    def test_not_asked_again_once_users_exist(self, tmp_path, monkeypatch, install):
        UserStore(_users_file(tmp_path)).add("root", "root-password", admin=True)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)

        def boom(_prompt):
            raise AssertionError("must not prompt")
        monkeypatch.setattr("builtins.input", boom)
        assert install.ensure_dashboard_admin(self._settings(tmp_path), False) is True


# ---------------------------------------------------------------- 2. CLI

class TestCliRegistration:
    def test_cli_user_can_log_in_over_http(self, tmp_path, monkeypatch):
        assert _cli(tmp_path, "add", "Alice", "--password-stdin",
                    stdin="alice-password\n", monkeypatch=monkeypatch) == 0
        d = _dashboard(tmp_path)

        async def script(client):
            ok = await _can_login(client, "alice", "alice-password")
            return ok, await (await client.get("/api/me")).json()
        ok, me = _run(d, script)
        assert ok and me == {"username": "alice", "admin": False, "via": "session"}

    def test_interactive_prompt_mismatch_fails(self, tmp_path, monkeypatch, capsys):
        answers = iter(["alice-password", "different-one"])
        monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))
        assert _cli(tmp_path, "add", "alice") == 1
        assert "do not match" in capsys.readouterr().err
        assert UserStore(_users_file(tmp_path)).get("alice") is None

    def test_every_management_command(self, tmp_path, monkeypatch, capsys):
        f = _users_file(tmp_path)
        assert _cli(tmp_path, "add", "root", "--admin", "--password-stdin",
                    stdin="root-password\n", monkeypatch=monkeypatch) == 0
        assert _cli(tmp_path, "add", "bob", "--password-stdin",
                    stdin="bob-password\n", monkeypatch=monkeypatch) == 0

        assert _cli(tmp_path, "passwd", "bob", "--password-stdin",
                    stdin="bob-password-2\n", monkeypatch=monkeypatch) == 0
        assert UserStore(f).authenticate("bob", "bob-password-2")

        assert _cli(tmp_path, "admin", "bob", "on") == 0
        assert UserStore(f).get("bob").admin
        assert _cli(tmp_path, "admin", "bob", "off") == 0
        assert not UserStore(f).get("bob").admin
        assert _cli(tmp_path, "admin", "bob", "maybe") == 1

        assert _cli(tmp_path, "disable", "bob") == 0
        assert UserStore(f).authenticate("bob", "bob-password-2") is None
        assert _cli(tmp_path, "enable", "bob") == 0
        assert UserStore(f).authenticate("bob", "bob-password-2")

        v = UserStore(f).get("bob").session_version
        assert _cli(tmp_path, "logout", "bob") == 0
        assert UserStore(f).get("bob").session_version == v + 1

        capsys.readouterr()
        assert _cli(tmp_path, "list") == 0
        listing = capsys.readouterr().out
        assert "root" in listing and "admin" in listing and "bob" in listing

        assert _cli(tmp_path, "delete", "bob") == 0
        assert UserStore(f).get("bob") is None
        assert _cli(tmp_path, "delete", "root") == 1          # last admin

    def test_passwd_unknown_user(self, tmp_path, monkeypatch, capsys):
        assert _cli(tmp_path, "passwd", "ghost", "--password-stdin",
                    stdin="whatever-1\n", monkeypatch=monkeypatch) == 1
        assert "no such user" in capsys.readouterr().err

    def test_token_commands(self, tmp_path, monkeypatch, capsys):
        _cli(tmp_path, "add", "alice", "--password-stdin",
             stdin="alice-password\n", monkeypatch=monkeypatch)
        capsys.readouterr()
        assert _cli(tmp_path, "token-add", "alice", "watcher") == 0
        token = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("pv_"))
        token_id = token.split("_")[1]

        assert _cli(tmp_path, "token-list", "alice") == 0
        listing = capsys.readouterr().out
        assert token_id in listing and "watcher" in listing and token not in listing

        d = _dashboard(tmp_path)
        h = {"Authorization": f"Bearer {token}"}

        async def script(client):
            before = (await client.get("/api/me", headers=h)).status
            rc = _cli(tmp_path, "token-revoke", "alice", token_id)
            _bump_mtime(_users_file(tmp_path))
            return before, rc, (await client.get("/api/me", headers=h)).status
        assert _run(d, script) == (200, 0, 401)
        assert _cli(tmp_path, "token-revoke", "alice", token_id) == 1

    def test_cli_change_applies_to_running_server(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path)                           # server already up
        _cli(tmp_path, "add", "alice", "--password-stdin",
             stdin="alice-password\n", monkeypatch=monkeypatch)
        _bump_mtime(_users_file(tmp_path))

        async def script(client):
            first = await _can_login(client, "alice", "alice-password")
            _cli(tmp_path, "disable", "alice")
            _bump_mtime(_users_file(tmp_path))
            return first, (await client.get("/api/me")).status
        assert _run(d, script) == (True, 401)

    def test_users_file_from_settings(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "settings.yaml").write_text(
            yaml.safe_dump({"http_server": {"users_file": str(tmp_path / "x.yaml")}}))
        monkeypatch.chdir(tmp_path)
        assert cli.resolve_users_file() == tmp_path / "x.yaml"
        assert cli.resolve_users_file("explicit.yaml") == Path("explicit.yaml")


# ---------------------------------------------------------------- 3. web page

class TestWebRegistration:
    @pytest.fixture
    def d(self, tmp_path):
        d = _dashboard(tmp_path)
        d.user_store.add("root", "root-password", admin=True)
        return d

    def test_admin_adds_user_who_then_logs_in(self, d):
        async def script(client):
            await _login(client, "root", "root-password")
            r = await client.post("/api/users", headers=CSRF, json={
                "username": "Carol", "password": "carol-password", "admin": True})
            async with TestClient(TestServer(d.app)) as other:
                ok = await _can_login(other, "carol", "carol-password")
                me = await (await other.get("/api/me")).json()
            return r.status, ok, me
        status, ok, me = _run(d, script)
        assert status == 200 and ok and me["admin"] is True

    def test_validation_errors(self, d):
        async def script(client):
            await _login(client, "root", "root-password")
            return [(await client.post("/api/users", headers=CSRF, json=body)).status
                    for body in ({"username": "x", "password": "long-enough"},
                                 {"username": "dave", "password": "short"},
                                 {"username": "root", "password": "long-enough"})]
        assert _run(d, script) == [400, 400, 400]

    def test_reset_password_and_logout_end_target_sessions(self, d):
        d.user_store.add("bob", "bob-password")

        async def script(client):
            await _login(client, "root", "root-password")
            async with TestClient(TestServer(d.app)) as bob:
                await _login(bob, "bob", "bob-password")
                before = (await bob.get("/api/me")).status
                await client.post("/api/users/bob/logout", headers=CSRF)
                after_logout = (await bob.get("/api/me")).status
                await _login(bob, "bob", "bob-password")
                await client.post("/api/users/bob/password", headers=CSRF,
                                  json={"password": "bob-new-password"})
                after_reset = (await bob.get("/api/me")).status
                old_pw = await _can_login(bob, "bob", "bob-password")
                new_pw = await _can_login(bob, "bob", "bob-new-password")
            return before, after_logout, after_reset, old_pw, new_pw
        assert _run(d, script) == (200, 401, 401, False, True)

    def test_toggle_admin_and_disable(self, d):
        d.user_store.add("bob", "bob-password")

        async def script(client):
            await _login(client, "root", "root-password")
            out = [(await client.patch("/api/users/bob", headers=CSRF,
                                       json={"admin": True})).status]
            out.append(d.user_store.get("bob").admin)
            out.append((await client.patch("/api/users/bob", headers=CSRF,
                                           json={"disabled": True})).status)
            async with TestClient(TestServer(d.app)) as bob:
                out.append(await _can_login(bob, "bob", "bob-password"))
            out.append((await client.patch("/api/users/ghost", headers=CSRF,
                                           json={"disabled": True})).status)
            return out
        assert _run(d, script) == [200, True, 200, False, 404]

    def test_admin_cannot_lock_themselves_out(self, d):
        async def script(client):
            await _login(client, "root", "root-password")
            return [
                (await client.patch("/api/users/root", headers=CSRF,
                                    json={"disabled": True})).status,
                (await client.patch("/api/users/root", headers=CSRF,
                                    json={"admin": False})).status,
                (await client.delete("/api/users/root", headers=CSRF)).status,
                (await client.get("/api/me")).status,
            ]
        assert _run(d, script) == [400, 400, 400, 200]

    def test_user_management_needs_admin_even_with_token(self, d):
        d.user_store.add("bob", "bob-password")
        token, _ = d.user_store.issue_token("bob", "script")
        h = {"Authorization": f"Bearer {token}"}

        async def script(client):
            return [(await client.get("/api/users", headers=h)).status,
                    (await client.post("/api/users", headers=h, json={
                        "username": "eve", "password": "eve-password"})).status,
                    (await client.delete("/api/users/root", headers=h)).status]
        assert _run(d, script) == [403, 403, 403]


# ---------------------------------------------------------------- 4. users.yaml by hand

class TestHandEditedYaml:
    def test_fresh_file_with_plaintext_works_over_http(self, tmp_path):
        f = _users_file(tmp_path)
        f.parent.mkdir(parents=True)
        f.write_text('users:\n  alice:\n    password: "hand-written"\n    admin: true\n')
        d = _dashboard(tmp_path)

        async def script(client):
            return await _can_login(client, "alice", "hand-written")
        assert _run(d, script)
        assert "hand-written" not in f.read_text()
        assert oct(f.stat().st_mode & 0o777) == "0o600"

    def test_added_while_running(self, tmp_path):
        d = _dashboard(tmp_path)
        d.user_store.add("root", "root-password", admin=True)
        f = _users_file(tmp_path)
        data = yaml.safe_load(f.read_text())
        data["users"]["bob"] = {"password": "bob-by-hand"}
        f.write_text(yaml.safe_dump(data))
        _bump_mtime(f)

        async def script(client):
            return await _can_login(client, "bob", "bob-by-hand")
        assert _run(d, script)

    def test_hand_reset_logs_user_out(self, tmp_path):
        d = _dashboard(tmp_path)
        d.user_store.add("alice", "alice-password")
        f = _users_file(tmp_path)

        async def script(client):
            await _login(client, "alice", "alice-password")
            before = (await client.get("/api/me")).status
            data = yaml.safe_load(f.read_text())
            data["users"]["alice"]["password"] = "reset-by-hand"
            f.write_text(yaml.safe_dump(data))
            _bump_mtime(f)
            after = (await client.get("/api/me")).status
            return before, after, await _can_login(client, "alice", "reset-by-hand")
        assert _run(d, script) == (200, 401, True)

    def test_hand_disable_and_admin_flags(self, tmp_path):
        d = _dashboard(tmp_path)
        d.user_store.add("root", "root-password", admin=True)
        d.user_store.add("bob", "bob-password")
        f = _users_file(tmp_path)
        data = yaml.safe_load(f.read_text())
        data["users"]["bob"]["admin"] = True
        f.write_text(yaml.safe_dump(data))
        _bump_mtime(f)
        assert d.user_store.get("bob").admin
        data["users"]["bob"]["disabled"] = True
        f.write_text(yaml.safe_dump(data))
        _bump_mtime(f)

        async def script(client):
            return await _can_login(client, "bob", "bob-password")
        assert _run(d, script) is False

    def test_broken_yaml_keeps_last_good_copy(self, tmp_path):
        d = _dashboard(tmp_path)
        d.user_store.add("alice", "alice-password")
        f = _users_file(tmp_path)
        f.write_text("users: [unclosed\n")
        _bump_mtime(f)

        async def script(client):
            return await _can_login(client, "alice", "alice-password")
        assert _run(d, script)

    def test_unwritable_file_still_authenticates(self, tmp_path, monkeypatch):
        f = _users_file(tmp_path)
        f.parent.mkdir(parents=True)
        f.write_text('users:\n  alice:\n    password: "hand-written"\n')
        store = UserStore(f)

        def fail_save():
            raise OSError("read-only")
        monkeypatch.setattr(store, "_save", fail_save)
        assert store.authenticate("alice", "hand-written") is not None

    def test_shipped_example_file(self, tmp_path):
        f = _users_file(tmp_path)
        f.parent.mkdir(parents=True)
        example = (REPO / "config" / "users.example.yaml").read_text()
        # Copied verbatim it must create no user at all (no published
        # default login); the first-run wizard then takes over.
        f.write_text(example)
        assert UserStore(f).list_users() == []
        # Uncommenting the documented entries works as described.
        import re
        body = example.split("users: {}", 1)[1]
        f.write_text("users:\n" + re.sub(r"(?m)^# ?", "", body))
        store = UserStore(f)
        assert {u.username for u in store.list_users()} == {"alice", "bob", "carol"}
        assert store.authenticate("alice", "change-me-now").admin
        assert store.authenticate("bob", "another-secret") is not None
        assert store.authenticate("carol", "anything") is None   # placeholder hash
        text = f.read_text()
        assert "change-me-now" not in text and "another-secret" not in text


# ---------------------------------------------------------------- sessions + tokens over HTTP

class TestSessionsAndTokens:
    @pytest.fixture
    def d(self, tmp_path):
        d = _dashboard(tmp_path)
        d.user_store.add("root", "root-password", admin=True)
        d.user_store.add("alice", "alice-password")
        return d

    def test_username_is_case_insensitive(self, d):
        assert _run(d, lambda c: _can_login(c, "  ALICE ", "alice-password"))

    def test_idle_expiry(self, d):
        clock = [time.time()]
        d.sessions._clock = lambda: clock[0]

        async def script(client):
            await _login(client, "alice", "alice-password")
            clock[0] += d.sessions.idle_s - 10
            ok = (await client.get("/api/me")).status
            clock[0] += d.sessions.idle_s + 10
            return ok, (await client.get("/api/me")).status
        assert _run(d, script) == (200, 401)

    def test_activity_refreshes_cookie(self, d):
        clock = [time.time()]
        d.sessions._clock = lambda: clock[0]

        async def script(client):
            await _login(client, "alice", "alice-password")
            early = await client.get("/api/me")
            clock[0] += REFRESH_AFTER_S + 1
            late = await client.get("/api/me")
            # keep active past what would have been the original idle deadline
            for _ in range(3):
                clock[0] += d.sessions.idle_s * 0.6
                last = await client.get("/api/me")
            return COOKIE_NAME in early.cookies, COOKIE_NAME in late.cookies, last.status
        assert _run(d, script) == (False, True, 200)

    def test_absolute_expiry_despite_activity(self, d):
        clock = [time.time()]
        d.sessions._clock = lambda: clock[0]

        async def script(client):
            await _login(client, "alice", "alice-password")
            status = 200
            while status == 200 and clock[0] < time.time() + d.sessions.max_s + 3600:
                clock[0] += d.sessions.idle_s * 0.5
                status = (await client.get("/api/me")).status
            return status, clock[0] - time.time() > d.sessions.max_s - d.sessions.idle_s
        assert _run(d, script) == (401, True)

    def test_forged_cookie_rejected(self, d):
        async def script(client):
            client.session.cookie_jar.update_cookies({COOKIE_NAME: "eyJ1Ijoicm9vdCJ9.AAAA"})
            return (await client.get("/api/me")).status
        assert _run(d, script) == 401

    def test_cookie_from_other_secret_rejected(self, d, tmp_path):
        other = _dashboard(tmp_path / "other")
        other.user_store.add("alice", "alice-password")

        async def get_cookie(client):
            r = await _login(client, "alice", "alice-password")
            return r.cookies[COOKIE_NAME].value
        cookie = _run(other, get_cookie)

        async def script(client):
            client.session.cookie_jar.update_cookies({COOKIE_NAME: cookie})
            return (await client.get("/api/me")).status
        assert _run(d, script) == 401

    def test_token_lifecycle_via_account_menu_api(self, d):
        async def script(client):
            await _login(client, "alice", "alice-password")
            empty = (await client.post("/api/me/tokens", headers=CSRF, json={})).status
            r = await (await client.post("/api/me/tokens", headers=CSRF,
                                         json={"name": "watcher"})).json()
            token, tid = r["token"], r["meta"]["id"]
            async with TestClient(TestServer(d.app)) as script_client:
                for hdr in ({"Authorization": f"Bearer {token}"}, {"X-Auth-Token": token}):
                    assert (await script_client.get("/api/me", headers=hdr)).status == 200
                # token requests need no CSRF header for mutations
                mut = (await script_client.post(
                    "/api/studies/1/actions/bogus",
                    headers={"Authorization": f"Bearer {token}"})).status
                missing = (await client.delete("/api/me/tokens/nope", headers=CSRF)).status
                await client.delete(f"/api/me/tokens/{tid}", headers=CSRF)
                revoked = (await script_client.get(
                    "/api/me", headers={"Authorization": f"Bearer {token}"})).status
            return empty, mut, missing, revoked
        assert _run(d, script) == (400, 400, 404, 401)

    def test_tokens_die_with_their_user(self, d):
        token, _ = d.user_store.issue_token("alice", "script")
        h = {"Authorization": f"Bearer {token}"}

        async def script(client):
            out = []
            for step in (None,
                         lambda: d.user_store.set_disabled("alice", True),
                         lambda: d.user_store.set_disabled("alice", False),
                         lambda: d.user_store.delete("alice")):
                if step:
                    step()
                out.append((await client.get("/api/me", headers=h)).status)
            return out
        assert _run(d, script) == [200, 401, 200, 401]

    def test_password_change_does_not_kill_tokens(self, d):
        token, _ = d.user_store.issue_token("alice", "script")
        d.user_store.set_password("alice", "alice-password-2")

        async def me(client):
            return (await client.get("/api/me",
                                     headers={"Authorization": f"Bearer {token}"})).status
        assert _run(d, me) == 200

    def test_one_users_token_cannot_act_as_another(self, d):
        token, _ = d.user_store.issue_token("alice", "script")
        root_token, _ = d.user_store.issue_token("root", "script")
        alice_id = token.split("_")[1]
        root_secret = root_token.split("_", 2)[2]

        async def me(client):
            r = await client.get("/api/me", headers={
                "Authorization": f"Bearer pv_{alice_id}_{root_secret}"})
            return r.status
        assert _run(d, me) == 401

    def test_token_actions_are_attributed_and_survive_user_deletion(self, d):
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        token, _ = d.user_store.issue_token("alice", "watcher")

        async def script(client):
            return (await client.post(
                "/api/studies/1/actions/mark_exported",
                headers={"Authorization": f"Bearer {token}"}, json={"via": "rhythm"})).status
        assert _run(d, script) == 200
        d.user_store.delete("alice")
        sc = StudySidecar.load(StudySidecar.path_for(sm.base_path, 1))
        assert sc.exported_by == "alice"
        assert sc.audit[-1]["user"] == "alice"

    def test_admin_link_only_for_admins(self, d):
        async def script(client):
            await _login(client, "alice", "alice-password")
            page = await client.get("/users", allow_redirects=False)
            return page.status, page.headers.get("Location")
        assert _run(d, script) == (302, "/")


class TestUsersPageNav:
    def test_nav_keeps_studies_badge_and_orders_users_before_about(self, tmp_path):
        d = _dashboard(tmp_path)
        d.user_store.add("root", "root-password", admin=True)

        async def script(client):
            await _login(client, "root", "root-password")
            return await (await client.get("/users")).text()
        html = _run(d, script)
        nav = html[html.index('<nav class="nav">'):html.index("</nav>")]
        assert 'id="nav-needs"' in nav
        studies, users, about = (nav.index(f'href="/{p}"') for p in ("studies", "users", "about"))
        assert studies < users < about
