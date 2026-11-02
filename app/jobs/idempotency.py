"""Khoá idempotent cho job: claim → làm → done; lỗi trước khi có tác dụng phụ ⇒ release để lần sau chạy lại."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import IdempotencyKey


class Idempotency:
    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self.sm = sm

    async def claim(self, key: str, scope: str) -> bool:
        """True nếu lần này được làm (chưa ai claim/done khoá này).

        Dùng RETURNING thay vì rowcount: với psycopg async, rowcount của INSERT ... ON CONFLICT có thể là -1.
        """
        async with self.sm() as s, s.begin():
            res = await s.execute(
                insert(IdempotencyKey)
                .values(key=key[:300], scope=scope, status="CLAIMED")
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(IdempotencyKey.key)
            )
            return res.scalar_one_or_none() is not None

    async def done(self, key: str, detail: dict[str, Any] | None = None) -> None:
        async with self.sm() as s, s.begin():
            await s.execute(
                update(IdempotencyKey)
                .where(IdempotencyKey.key == key[:300])
                .values(status="DONE", detail_json=detail)
            )

    async def release(self, key: str) -> None:
        async with self.sm() as s, s.begin():
            await s.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.key == key[:300], IdempotencyKey.status == "CLAIMED"
                )
            )

    async def forget(self, key: str) -> None:
        """Xoá khoá bất kể trạng thái (chỉ dùng khi chủ động gửi lại, ví dụ `--param resend=1`)."""
        async with self.sm() as s, s.begin():
            await s.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key[:300]))

    async def is_done(self, key: str) -> bool:
        async with self.sm() as s:
            row = await s.scalar(select(IdempotencyKey).where(IdempotencyKey.key == key[:300]))
            return row is not None
