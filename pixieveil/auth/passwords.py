"""Password hashing with stdlib scrypt (no extra dependency).

Stored format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`` — parameters
travel with the hash so they can be raised later without breaking old ones.
"""

import base64
import hashlib
import hmac
import logging
import secrets

logger = logging.getLogger(__name__)

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
MIN_PASSWORD_LENGTH = 8


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check; False (never an exception) on a malformed hash."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64),
                            n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError, AttributeError):
        logger.warning("Malformed password hash in user store")
        return False
    return hmac.compare_digest(dk, expected)


def validate_new_password(password: str) -> str | None:
    """Return an error message if the password is unacceptable, else None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None
