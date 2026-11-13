"""YAML-backed user store for the dashboard.

File layout (``config/users.yaml``, mode 0600)::

    users:
      alice:
        password_hash: "scrypt$16384$8$1$..."
        admin: true
        disabled: false
        created_at: "2026-11-13T10:00:00+00:00"
        last_login: null
        session_version: 1
        tokens:
          - id: "a1b2c3d4"
            name: "export-watcher"
            hash: "<sha256 hex of the secret>"
            created_at: "..."
            last_used: null

The file is re-read whenever its mtime changes, so edits made with the CLI
(or by hand) take effect on the running server without a restart. Writes
are atomic (tmp + rename). ``session_version`` is bumped whenever a user's
existing sessions must die (password change, disable, logout-everywhere).
"""

import copy
import hashlib
import hmac
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from pixieveil.auth import passwords

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("config/users.yaml")
TOKEN_PREFIX = "pv_"
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# last_used on API tokens is persisted at most this often per token, so a
# script polling every few seconds doesn't rewrite the YAML file each time.
_LAST_USED_PERSIST = timedelta(hours=1)


class UserStoreError(ValueError):
    """A user-management request that can't be honoured (bad name, last admin…)."""


@dataclass(frozen=True)
class User:
    """Read-only view of one account (never carries the password hash)."""

    username: str
    admin: bool = False
    disabled: bool = False
    created_at: str | None = None
    last_login: str | None = None
    session_version: int = 1
    tokens: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        """JSON-safe summary for the API (token hashes stripped)."""
        return {
            "username": self.username,
            "admin": self.admin,
            "disabled": self.disabled,
            "created_at": self.created_at,
            "last_login": self.last_login,
            "tokens": [
                {k: t.get(k) for k in ("id", "name", "created_at", "last_used")}
                for t in self.tokens
            ],
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not _USERNAME_RE.match(name):
        raise UserStoreError(
            "username must be 2-32 chars: lowercase letters, digits, '.', '_' or '-'"
        )
    return name


class UserStore:
    """Thread-safe accessor for the users YAML file."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._users: dict[str, dict[str, Any]] = {}
        self._mtime: float | None = None
        self._loaded = False

    # ------------------------------------------------------------ file I/O
    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is not None or not self._loaded:
                self._users, self._mtime = {}, None
            self._loaded = True
            return
        if self._loaded and mtime == self._mtime:
            return
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            # Keep the last good copy rather than locking everyone out on a
            # half-edited file.
            logger.error(f"Cannot read user store {self.path}: {e}")
            self._loaded = True
            return
        users = data.get("users") or {}
        if not isinstance(users, dict):
            logger.error(f"User store {self.path}: 'users' must be a mapping")
            users = {}
        self._users = {
            str(k).lower(): (v if isinstance(v, dict) else {}) for k, v in users.items()
        }
        self._mtime = mtime
        self._loaded = True
        self._hash_plaintext_passwords()

    def _hash_plaintext_passwords(self) -> None:
        """Replace any hand-written ``password: <plaintext>`` with a hash.

        Lets an admin add a user by editing users.yaml directly::

            users:
              bob:
                password: "initial-secret"

        On the next load the plaintext is hashed into ``password_hash``,
        missing fields get defaults, and the file is rewritten so the
        plaintext doesn't stay on disk.
        """
        changed = []
        for name, rec in self._users.items():
            if "password" not in rec:
                continue
            plain = rec.pop("password")
            if plain is None or str(plain) == "":
                logger.error(f"User store: empty 'password' for {name!r} ignored")
                continue
            if passwords.validate_new_password(str(plain)):
                logger.warning(f"User store: password set by hand for {name!r} is shorter "
                               f"than {passwords.MIN_PASSWORD_LENGTH} characters")
            rec["password_hash"] = passwords.hash_password(str(plain))
            rec["session_version"] = int(rec.get("session_version", 0) or 0) + 1
            rec.setdefault("admin", False)
            rec.setdefault("disabled", False)
            rec.setdefault("created_at", _now())
            rec.setdefault("last_login", None)
            rec.setdefault("tokens", [])
            changed.append(name)
        if not changed:
            return
        try:
            self._save()
            logger.info(f"Hashed plaintext password(s) in {self.path} for: {', '.join(changed)}")
        except OSError:
            logger.error(
                f"Hashed plaintext password(s) for {', '.join(changed)} in memory, but could "
                f"not rewrite {self.path} — the plaintext is still on disk"
            )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"users": self._users}
        fd, tmp = tempfile.mkstemp(prefix=".users.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("# PixieVeil dashboard users — manage with `python -m pixieveil.auth`\n")
                yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.error(f"Cannot write user store {self.path}: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._mtime = self.path.stat().st_mtime

    def _get_raw(self, username: str) -> dict[str, Any]:
        rec = self._users.get(username.lower())
        if rec is None:
            raise UserStoreError(f"no such user: {username}")
        return rec

    @staticmethod
    def _to_user(name: str, rec: dict[str, Any]) -> User:
        return User(
            username=name,
            admin=bool(rec.get("admin", False)),
            disabled=bool(rec.get("disabled", False)),
            created_at=rec.get("created_at"),
            last_login=rec.get("last_login"),
            session_version=int(rec.get("session_version", 1) or 1),
            tokens=copy.deepcopy(rec.get("tokens") or []),
        )

    def _enabled_admins(self) -> list[str]:
        return [n for n, r in self._users.items()
                if r.get("admin") and not r.get("disabled")]

    # ------------------------------------------------------------ queries
    def count(self) -> int:
        with self._lock:
            self._reload_if_changed()
            return len(self._users)

    def list_users(self) -> list[User]:
        with self._lock:
            self._reload_if_changed()
            return [self._to_user(n, r) for n, r in sorted(self._users.items())]

    def get(self, username: str) -> User | None:
        with self._lock:
            self._reload_if_changed()
            rec = self._users.get((username or "").lower())
            return self._to_user(username.lower(), rec) if rec is not None else None

    def get_active(self, username: str, session_version: int | None = None) -> User | None:
        """The user if it exists, is enabled and (optionally) the session version matches."""
        user = self.get(username)
        if user is None or user.disabled:
            return None
        if session_version is not None and user.session_version != session_version:
            return None
        return user

    # ------------------------------------------------------------ authentication
    def authenticate(self, username: str, password: str) -> User | None:
        """Verify a password login; stamps last_login on success."""
        with self._lock:
            self._reload_if_changed()
            name = (username or "").strip().lower()
            rec = self._users.get(name)
            if rec is None:
                # Burn comparable time so response timing doesn't reveal
                # which usernames exist.
                passwords.verify_password(password, passwords.hash_password("x"))
                return None
            if not passwords.verify_password(password, str(rec.get("password_hash", ""))):
                return None
            if rec.get("disabled"):
                return None
            rec["last_login"] = _now()
            try:
                self._save()
            except OSError:
                pass  # login still succeeds; last_login is cosmetic
            return self._to_user(name, rec)

    def verify_token(self, presented: str) -> User | None:
        """Resolve an API token ``pv_<id>_<secret>`` to its (enabled) owner."""
        if not presented or not presented.startswith(TOKEN_PREFIX):
            return None
        try:
            token_id, secret = presented[len(TOKEN_PREFIX):].split("_", 1)
        except ValueError:
            return None
        digest = _sha256(secret)
        with self._lock:
            self._reload_if_changed()
            for name, rec in self._users.items():
                for tok in rec.get("tokens") or []:
                    if tok.get("id") != token_id:
                        continue
                    if not hmac.compare_digest(str(tok.get("hash", "")), digest):
                        return None
                    if rec.get("disabled"):
                        return None
                    self._touch_token(tok)
                    return self._to_user(name, rec)
        return None

    def _touch_token(self, tok: dict[str, Any]) -> None:
        last = tok.get("last_used")
        now = datetime.now(timezone.utc)
        try:
            if last and now - datetime.fromisoformat(last) < _LAST_USED_PERSIST:
                return
        except ValueError:
            pass
        tok["last_used"] = now.isoformat(timespec="seconds")
        try:
            self._save()
        except OSError:
            pass

    # ------------------------------------------------------------ mutations
    def add(self, username: str, password: str, admin: bool = False) -> User:
        name = normalize_username(username)
        err = passwords.validate_new_password(password)
        if err:
            raise UserStoreError(err)
        with self._lock:
            self._reload_if_changed()
            if name in self._users:
                raise UserStoreError(f"user already exists: {name}")
            self._users[name] = {
                "password_hash": passwords.hash_password(password),
                "admin": bool(admin),
                "disabled": False,
                "created_at": _now(),
                "last_login": None,
                "session_version": 1,
                "tokens": [],
            }
            self._save()
            logger.info(f"User added: {name} (admin={bool(admin)})")
            return self._to_user(name, self._users[name])

    def set_password(self, username: str, password: str) -> None:
        err = passwords.validate_new_password(password)
        if err:
            raise UserStoreError(err)
        with self._lock:
            self._reload_if_changed()
            rec = self._get_raw(username)
            rec["password_hash"] = passwords.hash_password(password)
            rec["session_version"] = int(rec.get("session_version", 1) or 1) + 1
            self._save()
            logger.info(f"Password changed: {username.lower()}")

    def change_own_password(self, username: str, current: str, new: str) -> None:
        with self._lock:
            self._reload_if_changed()
            rec = self._get_raw(username)
            if not passwords.verify_password(current, str(rec.get("password_hash", ""))):
                raise UserStoreError("current password is incorrect")
            self.set_password(username, new)

    def set_admin(self, username: str, admin: bool) -> None:
        with self._lock:
            self._reload_if_changed()
            name = username.lower()
            rec = self._get_raw(name)
            if not admin and self._enabled_admins() == [name]:
                raise UserStoreError("cannot remove admin from the last enabled admin")
            rec["admin"] = bool(admin)
            self._save()
            logger.info(f"User {name}: admin={bool(admin)}")

    def set_disabled(self, username: str, disabled: bool) -> None:
        with self._lock:
            self._reload_if_changed()
            name = username.lower()
            rec = self._get_raw(name)
            if disabled and self._enabled_admins() == [name]:
                raise UserStoreError("cannot disable the last enabled admin")
            rec["disabled"] = bool(disabled)
            if disabled:
                rec["session_version"] = int(rec.get("session_version", 1) or 1) + 1
            self._save()
            logger.info(f"User {name}: disabled={bool(disabled)}")

    def revoke_sessions(self, username: str) -> None:
        """Log the user out everywhere (API tokens are unaffected)."""
        with self._lock:
            self._reload_if_changed()
            rec = self._get_raw(username)
            rec["session_version"] = int(rec.get("session_version", 1) or 1) + 1
            self._save()

    def delete(self, username: str) -> None:
        with self._lock:
            self._reload_if_changed()
            name = username.lower()
            self._get_raw(name)
            if self._enabled_admins() == [name]:
                raise UserStoreError("cannot delete the last enabled admin")
            del self._users[name]
            self._save()
            logger.info(f"User deleted: {name}")

    def issue_token(self, username: str, name: str) -> tuple[str, dict[str, Any]]:
        """Create an API token; returns (plaintext — shown once, metadata)."""
        label = (name or "").strip()[:64]
        if not label:
            raise UserStoreError("token name is required")
        with self._lock:
            self._reload_if_changed()
            rec = self._get_raw(username)
            tokens = rec.setdefault("tokens", [])
            existing = {t.get("id") for t in tokens}
            token_id = secrets.token_hex(4)
            while token_id in existing:
                token_id = secrets.token_hex(4)
            secret = secrets.token_urlsafe(32)
            meta = {"id": token_id, "name": label, "hash": _sha256(secret),
                    "created_at": _now(), "last_used": None}
            tokens.append(meta)
            self._save()
            logger.info(f"API token issued: user={username.lower()} id={token_id} name={label!r}")
            public = {k: meta[k] for k in ("id", "name", "created_at", "last_used")}
            return f"{TOKEN_PREFIX}{token_id}_{secret}", public

    def revoke_token(self, username: str, token_id: str) -> None:
        with self._lock:
            self._reload_if_changed()
            rec = self._get_raw(username)
            tokens = rec.get("tokens") or []
            kept = [t for t in tokens if t.get("id") != token_id]
            if len(kept) == len(tokens):
                raise UserStoreError(f"no such token: {token_id}")
            rec["tokens"] = kept
            self._save()
            logger.info(f"API token revoked: user={username.lower()} id={token_id}")
