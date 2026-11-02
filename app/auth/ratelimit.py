"""Giới hạn tần suất dùng chung giữa các bản api (bảng rate_limits, cửa sổ cố định)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.db.models import RateLimit
from app.errors import RateLimited


def _window(now: datetime, seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=now.tzinfo)


async def hit(s: AsyncSession, key: str, limit: int, window_s: int) -> int:
    """Tăng bộ đếm; vượt `limit` ⇒ RateLimited. Trả về số lần trong cửa sổ hiện tại."""
    ws = _window(clock.now(), window_s)
    ins = insert(RateLimit).values(key=key[:200], window_start=ws, count=1)
    stmt = ins.on_conflict_do_update(
        index_elements=["key", "window_start"], set_={"count": RateLimit.count + 1}
    ).returning(RateLimit.count)
    count = int((await s.execute(stmt)).scalar_one())
    if count > limit:
        raise RateLimited(f"rate limit {key}: {count}/{limit}")
    return count


async def peek(s: AsyncSession, key: str, window_s: int) -> int:
    ws = _window(clock.now(), window_s)
    row = await s.get(RateLimit, (key[:200], ws))
    return row.count if row else 0


async def purge(s: AsyncSession, older_than: timedelta = timedelta(days=2)) -> None:
    await s.execute(delete(RateLimit).where(RateLimit.window_start < clock.now() - older_than))
