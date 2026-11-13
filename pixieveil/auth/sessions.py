"""Stateless, HMAC-signed session cookies.

Cookie value: ``<payload_b64>.<sig_b64>`` where payload is JSON
``{"u": username, "v": session_version, "iat": issued_at, "lat": last_active}``.
The signing secret lives in a 0600 file next to the users file and is
created on first use, so sessions survive restarts. Revocation is via the
user's ``session_version`` in the user store (password change, disable,
logout-everywhere bump it).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

COOKIE_NAME = "pv_session"
# The cookie is re-issued (sliding idle window) at most this often.
REFRESH_AFTER_S = 300


def load_or_create_secret(path: Path) -> bytes:
    """Read the signing secret, creating a random one (mode 0600) if absent."""
    try:
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
        logger.warning(f"Session secret {path} too short; regenerating")
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32).encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secret)
    logger.info(f"Created session secret {path}")
    return secret


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass(frozen=True)
class Session:
    username: str
    version: int
    issued_at: float
    last_active: float


class SessionSigner:
    def __init__(self, secret: bytes, idle_s: float, max_s: float,
                 clock: Callable[[], float] = time.time) -> None:
        self._secret = secret
        self.idle_s = idle_s
        self.max_s = max_s
        self._clock = clock

    def _sign(self, payload: bytes) -> str:
        return _b64e(hmac.new(self._secret, payload, hashlib.sha256).digest())

    def issue(self, username: str, version: int, issued_at: float | None = None) -> str:
        now = self._clock()
        payload = json.dumps({"u": username, "v": version,
                              "iat": int(issued_at if issued_at is not None else now),
                              "lat": int(now)}, separators=(",", ":")).encode()
        return f"{_b64e(payload)}.{self._sign(payload)}"

    def verify(self, value: str | None) -> Session | None:
        """Decode and check signature + idle/absolute expiry; None if invalid."""
        if not value or "." not in value:
            return None
        p64, sig = value.rsplit(".", 1)
        try:
            payload = _b64d(p64)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        try:
            d = json.loads(payload)
            sess = Session(str(d["u"]), int(d["v"]), float(d["iat"]), float(d["lat"]))
        except (ValueError, KeyError, TypeError):
            return None
        now = self._clock()
        if now - sess.last_active > self.idle_s or now - sess.issued_at > self.max_s:
            return None
        return sess

    def needs_refresh(self, sess: Session) -> bool:
        return self._clock() - sess.last_active >= REFRESH_AFTER_S

    def cookie_max_age(self, sess_issued_at: float | None = None) -> int:
        """Cookie lifetime: the idle window, capped by the absolute limit."""
        if sess_issued_at is None:
            return int(min(self.idle_s, self.max_s))
        remaining = self.max_s - (self._clock() - sess_issued_at)
        return max(0, int(min(self.idle_s, remaining)))
