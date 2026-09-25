"""Shared-password login with an HMAC-signed, expiring session cookie."""

from __future__ import annotations

import hashlib
import hmac
import time

COOKIE = "mrpa_session"
SESSION_SECONDS = 30 * 24 * 3600


def check_password(given: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def issue(secret: bytes, now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_SECONDS)
    return f"{expires}.{_sign(secret, str(expires))}"


def valid(secret: bytes, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(secret, expires))
