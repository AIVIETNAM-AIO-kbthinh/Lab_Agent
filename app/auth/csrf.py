"""CSRF double-submit: cookie `la_csrf` (JS đọc được) + header `X-CSRF-Token`, ràng buộc HMAC với session."""

from __future__ import annotations

import hashlib
import hmac

COOKIE = "la_csrf"
HEADER = "x-csrf-token"


def make_token(secret: str, session_public_id: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"csrf:{session_public_id}".encode(), hashlib.sha256).hexdigest()


def check(secret: str, session_public_id: str, cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    expected = make_token(secret, session_public_id)
    return hmac.compare_digest(cookie_value, header_value) and hmac.compare_digest(header_value, expected)
