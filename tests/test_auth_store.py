"""UserStore (YAML user file), password hashing and the auth CLI."""

import os
import stat
import time

import pytest
import yaml

from pixieveil.auth import cli, passwords
from pixieveil.auth.user_store import UserStore, UserStoreError


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.yaml")


class TestPasswords:
    def test_roundtrip(self):
        h = passwords.hash_password("correct horse")
        assert h.startswith("scrypt$")
        assert passwords.verify_password("correct horse", h)
        assert not passwords.verify_password("wrong horse", h)

    def test_salted(self):
        assert passwords.hash_password("x" * 8) != passwords.hash_password("x" * 8)

    @pytest.mark.parametrize("bad", ["", "plain", "bcrypt$1$2$3$a$b", "scrypt$x$8$1$AA==$AA=="])
    def test_malformed_hash_is_false_not_error(self, bad):
        assert passwords.verify_password("anything", bad) is False


class TestUserStore:
    def test_add_and_authenticate(self, store):
        store.add("Alice", "password1", admin=True)
        user = store.authenticate("alice", "password1")
        assert user is not None and user.username == "alice" and user.admin
        assert user.last_login is not None
        assert store.authenticate("alice", "nope-nope") is None
        assert store.authenticate("nobody", "password1") is None

    def test_file_is_private_and_has_no_plaintext(self, store):
        store.add("alice", "password1")
        mode = stat.S_IMODE(os.stat(store.path).st_mode)
        assert mode == 0o600
        text = store.path.read_text()
        assert "password1" not in text
        data = yaml.safe_load(text)
        assert data["users"]["alice"]["password_hash"].startswith("scrypt$")

    def test_reload_after_external_edit(self, store, tmp_path):
        store.add("alice", "password1")
        other = UserStore(store.path)          # e.g. the CLI in another process
        time.sleep(0.01)
        other.add("bob", "password2")
        os.utime(store.path, (time.time() + 5, time.time() + 5))  # force a distinct mtime
        assert {u.username for u in store.list_users()} == {"alice", "bob"}

    @pytest.mark.parametrize("name", ["a", "has space", "-lead", "x" * 33, "ünï"])
    def test_rejects_bad_usernames(self, store, name):
        with pytest.raises(UserStoreError):
            store.add(name, "password1")

    def test_rejects_short_password_and_duplicates(self, store):
        with pytest.raises(UserStoreError):
            store.add("alice", "short")
        store.add("alice", "password1")
        with pytest.raises(UserStoreError, match="already exists"):
            store.add("ALICE", "password1")

    def test_disabled_user_cannot_log_in(self, store):
        store.add("root", "password0", admin=True)
        store.add("alice", "password1")
        store.set_disabled("alice", True)
        assert store.authenticate("alice", "password1") is None
        assert store.get_active("alice") is None

    def test_last_admin_is_protected(self, store):
        store.add("root", "password0", admin=True)
        store.add("alice", "password1")
        for op in (lambda: store.set_disabled("root", True),
                   lambda: store.set_admin("root", False),
                   lambda: store.delete("root")):
            with pytest.raises(UserStoreError, match="last enabled admin"):
                op()
        store.set_admin("alice", True)
        store.delete("root")                    # fine once another admin exists
        assert store.get("root") is None

    def test_password_change_and_disable_bump_session_version(self, store):
        store.add("root", "password0", admin=True)
        store.add("alice", "password1")
        v1 = store.get("alice").session_version
        store.set_password("alice", "password2")
        v2 = store.get("alice").session_version
        assert v2 == v1 + 1
        assert store.get_active("alice", v1) is None
        assert store.get_active("alice", v2) is not None
        store.revoke_sessions("alice")
        assert store.get("alice").session_version == v2 + 1

    def test_change_own_password_checks_current(self, store):
        store.add("alice", "password1")
        with pytest.raises(UserStoreError, match="incorrect"):
            store.change_own_password("alice", "wrong-one", "password2")
        store.change_own_password("alice", "password1", "password2")
        assert store.authenticate("alice", "password2") is not None


class TestHandWrittenPlaintext:
    def test_plaintext_is_hashed_on_first_access(self, tmp_path):
        path = tmp_path / "users.yaml"
        path.write_text("users:\n  Bob:\n    password: hand-written-1\n    admin: true\n")
        store = UserStore(path)
        user = store.authenticate("bob", "hand-written-1")
        assert user is not None and user.admin and not user.disabled
        text = path.read_text()
        assert "hand-written-1" not in text
        rec = yaml.safe_load(text)["users"]["bob"]
        assert "password" not in rec and rec["password_hash"].startswith("scrypt$")
        assert rec["tokens"] == [] and rec["created_at"]
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_plaintext_added_to_existing_file_while_running(self, store):
        store.add("alice", "password1", admin=True)
        data = yaml.safe_load(store.path.read_text())
        data["users"]["carol"] = {"password": "carol-pass1"}
        store.path.write_text(yaml.safe_dump(data))
        os.utime(store.path, (time.time() + 5, time.time() + 5))
        assert store.authenticate("carol", "carol-pass1") is not None
        assert "carol-pass1" not in store.path.read_text()
        assert store.authenticate("alice", "password1") is not None

    def test_resetting_password_by_hand_ends_sessions(self, store):
        store.add("alice", "password1")
        v = store.get("alice").session_version
        data = yaml.safe_load(store.path.read_text())
        data["users"]["alice"]["password"] = "replaced-1"
        store.path.write_text(yaml.safe_dump(data))
        os.utime(store.path, (time.time() + 5, time.time() + 5))
        assert store.get("alice").session_version == v + 1
        assert store.authenticate("alice", "replaced-1") is not None
        assert store.authenticate("alice", "password1") is None

    def test_empty_plaintext_is_ignored(self, tmp_path):
        path = tmp_path / "users.yaml"
        path.write_text("users:\n  bob:\n    password: ''\n")
        assert UserStore(path).authenticate("bob", "") is None


class TestApiTokens:
    def test_issue_verify_revoke(self, store):
        store.add("alice", "password1")
        token, meta = store.issue_token("alice", "watcher")
        assert token.startswith(f"pv_{meta['id']}_")
        assert token not in store.path.read_text()
        user = store.verify_token(token)
        assert user is not None and user.username == "alice"
        assert store.get("alice").tokens[0]["last_used"] is not None
        assert "hash" not in store.get("alice").public()["tokens"][0]
        store.revoke_token("alice", meta["id"])
        assert store.verify_token(token) is None

    @pytest.mark.parametrize("bad", ["", "pv_", "pv_nounderscore", "Bearer x", "pv_abcd_wrong"])
    def test_garbage_tokens(self, store, bad):
        store.add("alice", "password1")
        store.issue_token("alice", "watcher")
        assert store.verify_token(bad) is None

    def test_wrong_secret_for_real_id(self, store):
        store.add("alice", "password1")
        token, meta = store.issue_token("alice", "watcher")
        assert store.verify_token(f"pv_{meta['id']}_not-the-secret") is None

    def test_disabled_users_tokens_stop_working(self, store):
        store.add("root", "password0", admin=True)
        store.add("alice", "password1")
        token, _ = store.issue_token("alice", "watcher")
        store.set_disabled("alice", True)
        assert store.verify_token(token) is None


class TestCli:
    def test_add_list_token_flow(self, tmp_path, monkeypatch, capsys):
        f = str(tmp_path / "users.yaml")
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("password1\n"))
        assert cli.main(["--file", f, "add", "alice", "--admin", "--password-stdin"]) == 0
        assert cli.main(["--file", f, "token-add", "alice", "ci"]) == 0
        out = capsys.readouterr().out
        token = next(line for line in out.splitlines() if line.startswith("pv_"))
        assert UserStore(f).verify_token(token).username == "alice"
        assert cli.main(["--file", f, "list"]) == 0
        assert "alice" in capsys.readouterr().out

    def test_errors_exit_nonzero(self, tmp_path, capsys):
        f = str(tmp_path / "users.yaml")
        assert cli.main(["--file", f, "disable", "ghost"]) == 1
        assert "no such user" in capsys.readouterr().err
