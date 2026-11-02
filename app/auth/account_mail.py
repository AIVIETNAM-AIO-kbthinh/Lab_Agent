"""Gửi mật khẩu tạm qua mail (admin tạo / reset tài khoản, hoặc hệ thống tự tạo khi duyệt đơn gia nhập)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging import get_logger
from app.mail.mailer import OutgoingMail, Recipient
from app.mail.templates import render

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


async def send_temp_password(
    svc: Services, email: str, temp_password: str, login_url: str | None = None, name: str | None = None
) -> bool:
    """True = đã gửi. Lỗi gửi mail không ném ra: caller hiện mật khẩu tạm cho admin."""
    subject, body = render(
        "account_created",
        lab_name=svc.config.lab.name,
        email=email,
        temp_password=temp_password,
        login_url=login_url or svc.config.auth.login_url or "trang đăng nhập Lab Agent",
    )
    try:
        await svc.mailer.send(OutgoingMail([Recipient(email, name or "")], subject, body))
        return True
    except Exception as e:
        log.warning("temp_password_mail_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return False


async def is_lab_email(svc: Services, email: str) -> bool:
    """Email thuộc admins (config) hoặc thành viên đang hoạt động trong Members (đọc mới, không cache)."""
    if svc.config.auth.admin(email) is not None:
        return True
    snap = await svc.members.snapshot(force=True)
    return snap.by_email(email) is not None
