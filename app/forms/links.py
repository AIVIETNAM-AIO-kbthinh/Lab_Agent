"""Link tải đơn trống có chữ ký HMAC + hạn dùng (không gửi link Drive cho guest — TK §7.4)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services import Services


def _sig(secret: str, form_id: str, exp: int) -> str:
    return hmac.new(secret.encode(), f"blank:{form_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def blank_url(svc: Services, form_id: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + svc.config.drafts.blank_link_ttl_minutes * 60)
    sig = _sig(svc.settings.app_secret_key.get_secret_value(), form_id, exp)
    return f"/forms/{form_id}/blank?exp={exp}&sig={sig}"


def verify_blank(svc: Services, form_id: str, exp: int, sig: str, now: float | None = None) -> bool:
    if exp < (now or time.time()):
        return False
    good = _sig(svc.settings.app_secret_key.get_secret_value(), form_id, exp)
    return hmac.compare_digest(good, sig)
