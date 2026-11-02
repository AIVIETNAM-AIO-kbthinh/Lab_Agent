"""Session: token ngẫu nhiên 256 bit trong cookie HttpOnly; DB chỉ lưu SHA-256 của token.

Hết hạn khi không hoạt động `session_idle_days`, tối đa `session_max_days`. Đổi mật khẩu ⇒ huỷ mọi session.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.config import AuthCfg
from app.db.models import Session

COOKIE = "la_session"
GUEST_DAYS = 1
TOUCH_EVERY = timedelta(minutes=5)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def create_session(
    s: AsyncSession,
    cfg: AuthCfg,
    *,
    account_id: str | None = None,
    dev_login_name: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)  # 256 bit
    now = clock.now()
    guest = account_id is None and dev_login_name is None
    days = GUEST_DAYS if guest else cfg.session_max_days
    row = Session(
        id_hash=token_hash(token),
        account_id=account_id,
        guest=guest,
        dev_login_name=dev_login_name,
        ip=(ip or "")[:64],
        user_agent=(user_agent or "")[:300],
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=days),
    )
    s.add(row)
    await s.flush()
    return token, row


async def load_session(s: AsyncSession, cfg: AuthCfg, token: str | None) -> Session | None:
    if not token or len(token) > 100:
        return None
    row = await s.get(Session, token_hash(token))
    if row is None:
        return None
    now = clock.now()
    idle = timedelta(days=GUEST_DAYS if row.guest else cfg.session_idle_days)
    if row.expires_at <= now or row.last_seen_at + idle <= now:
        await s.delete(row)
        return None
    if now - row.last_seen_at > TOUCH_EVERY:
        row.last_seen_at = now
    return row


async def revoke(s: AsyncSession, token: str) -> None:
    await s.execute(delete(Session).where(Session.id_hash == token_hash(token)))


async def revoke_all(s: AsyncSession, account_id: str, except_hash: str | None = None) -> int:
    q = delete(Session).where(Session.account_id == account_id)
    if except_hash:
        q = q.where(Session.id_hash != except_hash)
    res = await s.execute(q)
    return int(getattr(res, "rowcount", 0) or 0)


async def purge_expired(s: AsyncSession) -> int:
    res = await s.execute(delete(Session).where(Session.expires_at <= clock.now()))
    return int(getattr(res, "rowcount", 0) or 0)


async def sessions_of(s: AsyncSession, account_id: str) -> list[Session]:
    return list((await s.scalars(select(Session).where(Session.account_id == account_id))).all())
