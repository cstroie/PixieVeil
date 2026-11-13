"""Signed session cookies: signature, idle/absolute expiry, secret file."""

import os
import stat

from pixieveil.auth.sessions import SessionSigner, load_or_create_secret


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _signer(clock, idle=3600, max_s=86400):
    return SessionSigner(b"k" * 64, idle_s=idle, max_s=max_s, clock=clock)


def test_roundtrip():
    s = _signer(Clock())
    sess = s.verify(s.issue("alice", 3))
    assert sess.username == "alice" and sess.version == 3


def test_tampered_payload_or_signature_rejected():
    s = _signer(Clock())
    cookie = s.issue("alice", 1)
    payload, sig = cookie.rsplit(".", 1)
    forged = s.issue("mallory", 1).rsplit(".", 1)[0]
    assert s.verify(f"{forged}.{sig}") is None
    assert s.verify(f"{payload}.{sig[:-2]}AA") is None
    assert s.verify("garbage") is None and s.verify(None) is None


def test_other_secret_rejected():
    clock = Clock()
    cookie = _signer(clock).issue("alice", 1)
    other = SessionSigner(b"z" * 64, idle_s=3600, max_s=86400, clock=clock)
    assert other.verify(cookie) is None


def test_idle_expiry_and_sliding_refresh():
    clock = Clock()
    s = _signer(clock, idle=3600)
    cookie = s.issue("alice", 1)
    clock.t += 3000
    sess = s.verify(cookie)
    assert sess is not None and s.needs_refresh(sess)
    refreshed = s.issue("alice", 1, issued_at=sess.issued_at)
    clock.t += 3000                      # 6000s since login, 3000s idle
    assert s.verify(cookie) is None
    assert s.verify(refreshed) is not None


def test_absolute_expiry_despite_activity():
    clock = Clock()
    s = _signer(clock, idle=3600, max_s=7200)
    cookie = s.issue("alice", 1)
    iat = s.verify(cookie).issued_at
    for _ in range(3):
        clock.t += 3000
        cookie = s.issue("alice", 1, issued_at=iat)
    assert s.verify(cookie) is None


def test_secret_file_created_private_and_reused(tmp_path):
    p = tmp_path / "sub" / ".session_secret"
    a = load_or_create_secret(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert load_or_create_secret(p) == a and len(a) >= 32
