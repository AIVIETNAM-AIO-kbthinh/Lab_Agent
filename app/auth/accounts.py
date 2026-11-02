"""Tài khoản do admin cấp: tạo (mật khẩu tạm), đăng nhập có khoá tạm tăng dần, đổi/reset mật khẩu."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.auth import sessions
from app.auth.passwords import (
    dummy_verify,
    generate_temp_password,
    hash_password,
    policy_errors,
    verify_password,
)
from app.config import AuthCfg
from app.db.models import Account
from app.domain.names import valid_email
from app.errors import LabAgentError, NotFound, ValidationFailed

GENERIC_LOGIN_ERROR = "Email hoặc mật khẩu không đúng, hoặc tài khoản đang bị khoá tạm thời."


class LoginFailed(LabAgentError):
    user_message = GENERIC_LOGIN_ERROR


def norm_email(email: str) -> str:
    return (email or "").strip().lower()


async def get_by_email(s: AsyncSession, email: str) -> Account | None:
    return await s.scalar(select(Account).where(func.lower(Account.email) == norm_email(email)))


async def create_account(s: AsyncSession, email: str, password: str | None = None) -> tuple[Account, str]:
    e = norm_email(email)
    if not valid_email(e):
        raise ValidationFailed(f"Email không hợp lệ: {email}")
    if await get_by_email(s, e):
        raise ValidationFailed(f"Tài khoản {e} đã tồn tại")
    pw = password or generate_temp_password()
    acc = Account(email=e, password_hash=hash_password(pw), must_change_password=True)
    s.add(acc)
    await s.flush()
    return acc, pw


async def reset_password(s: AsyncSession, email: str) -> str:
    acc = await get_by_email(s, email)
    if acc is None:
        raise NotFound(f"Không có tài khoản {email}")
    pw = generate_temp_password()
    acc.password_hash = hash_password(pw)
    acc.must_change_password = True
    acc.failed_logins = 0
    acc.locked_until = None
    acc.lock_level = 0
    await sessions.revoke_all(s, acc.id)
    return pw


async def set_disabled(s: AsyncSession, email: str, disabled: bool) -> None:
    acc = await get_by_email(s, email)
    if acc is None:
        raise NotFound(f"Không có tài khoản {email}")
    acc.disabled = disabled
    if disabled:
        await sessions.revoke_all(s, acc.id)


async def unlock(s: AsyncSession, email: str) -> None:
    acc = await get_by_email(s, email)
    if acc is None:
        raise NotFound(f"Không có tài khoản {email}")
    acc.failed_logins, acc.locked_until, acc.lock_level = 0, None, 0


@dataclass
class LoginResult:
    account: Account
    must_change_password: bool


async def authenticate(s: AsyncSession, cfg: AuthCfg, email: str, password: str) -> LoginResult | None:
    """Sai `login_max_failures` lần ⇒ khoá `login_lock_minutes` × 2^lock_level.

    Trả None khi thất bại (KHÔNG ném lỗi) để caller commit bộ đếm sai trước khi báo lỗi chung chung.
    """
    acc = await get_by_email(s, email)
    now = clock.now()
    if acc is None or acc.disabled:
        dummy_verify()
        return None
    if acc.locked_until and acc.locked_until > now:
        dummy_verify()
        return None
    ok, rehash = verify_password(acc.password_hash, password)
    if not ok:
        acc.failed_logins += 1
        if acc.failed_logins >= cfg.login_max_failures:
            acc.locked_until = now + timedelta(minutes=cfg.login_lock_minutes * (2 ** min(acc.lock_level, 6)))
            acc.lock_level += 1
            acc.failed_logins = 0
        return None
    if rehash:
        acc.password_hash = hash_password(password)
    acc.failed_logins = 0
    acc.lock_level = 0
    acc.locked_until = None
    acc.last_login_at = now
    return LoginResult(acc, acc.must_change_password)


async def change_password(
    s: AsyncSession, cfg: AuthCfg, account_id: str, old: str, new: str, keep_session_hash: str | None = None
) -> None:
    acc = await s.get(Account, account_id)
    if acc is None:
        raise NotFound("account")
    ok, _ = verify_password(acc.password_hash, old)
    if not ok:
        raise ValidationFailed("old password", user_message="Mật khẩu hiện tại không đúng.")
    errs = policy_errors(new, min_length=cfg.password_min_length, email=acc.email)
    if old == new:
        errs.append("Mật khẩu mới phải khác mật khẩu cũ.")
    if errs:
        raise ValidationFailed("policy", user_message=" ".join(errs))
    acc.password_hash = hash_password(new)
    acc.must_change_password = False
    acc.password_changed_at = clock.now()
    await sessions.revoke_all(s, acc.id, except_hash=keep_session_hash)


async def delete_account(s: AsyncSession, email: str) -> None:
    """Xoá tài khoản và mọi session của nó. AuditLog (trên Drive) vẫn giữ email."""
    acc = await get_by_email(s, email)
    if acc is None:
        raise NotFound(f"Không có tài khoản {email}")
    await sessions.revoke_all(s, acc.id)
    await s.delete(acc)


async def ensure_account(s: AsyncSession, email: str) -> tuple[Account, str | None]:
    """Tạo tài khoản nếu chưa có (duyệt đơn gia nhập). Trả (tài khoản, mật khẩu tạm | None nếu đã có)."""
    acc = await get_by_email(s, email)
    if acc is not None:
        return acc, None
    return await create_account(s, email)


async def list_accounts(s: AsyncSession) -> list[Account]:
    return list((await s.scalars(select(Account).order_by(Account.email))).all())
