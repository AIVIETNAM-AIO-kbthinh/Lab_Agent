"""Bản nháp đơn trong DB (chưa xác nhận thì chưa ghi gì lên Drive/Sheets — TK §0 nguyên tắc 3).

Trạng thái: COLLECTING → PREVIEWED (agent điền) | READY (file tự điền hợp lệ) → CONFIRMING → SUBMITTED
            · CANCELLED · FAILED. Hết hạn (expires_at) ⇒ không dùng được, job draft_cleanup xoá.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import clock
from app.auth.roles import UserContext
from app.db.models import Draft
from app.errors import DraftInvalid

OPEN = ("COLLECTING", "PREVIEWED", "READY")
CONFIRMABLE = ("PREVIEWED", "READY")


class DraftStore:
    def __init__(self, sm: async_sessionmaker[AsyncSession], ttl_hours: int = 24) -> None:
        self.sm = sm
        self.ttl_hours = ttl_hours

    async def create(self, **values: Any) -> Draft:
        d = Draft(expires_at=clock.now() + timedelta(hours=self.ttl_hours), **values)
        async with self.sm() as s, s.begin():
            s.add(d)
            await s.flush()
            s.expunge(d)
        return d

    async def load(self, draft_id: str) -> Draft | None:
        async with self.sm() as s:
            d = await s.get(Draft, draft_id)
            if d is not None:
                s.expunge(d)
            return d

    async def owned(self, draft_id: str, ctx: UserContext, *, allow_closed: bool = False) -> Draft:
        """Bản nháp của đúng session này, còn hạn. Sai chủ cũng báo 'không còn hiệu lực' (không lộ tồn tại)."""
        d = await self.load(draft_id)
        if d is None or d.owner_session != ctx.session_id:
            raise DraftInvalid(f"draft {draft_id}: không tồn tại hoặc không thuộc session")
        if d.expires_at <= clock.now():
            raise DraftInvalid(f"draft {draft_id}: hết hạn")
        if not allow_closed and d.status not in OPEN:
            raise DraftInvalid(f"draft {draft_id}: trạng thái {d.status}")
        return d

    async def find_open(self, session_id: str, form_id: str) -> Draft | None:
        async with self.sm() as s:
            d = await s.scalar(
                select(Draft)
                .where(
                    Draft.owner_session == session_id,
                    Draft.form_id == form_id,
                    Draft.status.in_(OPEN),
                    Draft.expires_at > clock.now(),
                )
                .order_by(Draft.updated_at.desc())
                .limit(1)
            )
            if d is not None:
                s.expunge(d)
            return d

    async def save(self, d: Draft) -> None:
        async with self.sm() as s, s.begin():
            await s.merge(d)

    async def set(self, draft_id: str, **values: Any) -> None:
        async with self.sm() as s, s.begin():
            await s.execute(update(Draft).where(Draft.id == draft_id).values(**values))

    async def lock_for_confirm(self, draft_id: str, session_id: str, shown_hash: str) -> bool:
        """Chống bấm đúp: chỉ MỘT request chuyển được sang CONFIRMING."""
        async with self.sm() as s, s.begin():
            res = await s.execute(
                update(Draft)
                .where(
                    Draft.id == draft_id,
                    Draft.owner_session == session_id,
                    Draft.status.in_(CONFIRMABLE),
                    Draft.shown_hash == shown_hash,
                    Draft.expires_at > clock.now(),
                )
                .values(status="CONFIRMING")
                .returning(Draft.id)
            )
            return res.scalar_one_or_none() is not None
