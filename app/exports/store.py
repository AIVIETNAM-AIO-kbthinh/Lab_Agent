"""Lưu file xuất vào DB, tải lại theo đúng session đã yêu cầu."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete

from app import clock
from app.auth.roles import UserContext
from app.db.models import Export
from app.exports.build import Table
from app.exports.files import render

if TYPE_CHECKING:
    from app.services import Services


async def save(svc: Services, ctx: UserContext, table: Table, fmt: str) -> Export:
    data, mime, ext = render(table, fmt, actor=ctx.actor)
    return await save_bytes(
        svc, ctx, table.dataset, f"{table.file_stem}.{ext}", mime, data, len(table.rows), table.filters
    )


async def save_bytes(
    svc: Services,
    ctx: UserContext,
    dataset: str,
    filename: str,
    mime: str,
    data: bytes,
    count: int,
    filters: dict[str, str],
) -> Export:
    now = clock.now()
    e = Export(
        owner_session=ctx.session_id,
        actor=ctx.actor[:200],
        dataset=dataset[:40],
        filename=filename[:255],
        mime=mime,
        data=data,
        row_count=count,
        filters_json=dict(filters),
        created_at=now,
        expires_at=now + timedelta(minutes=svc.config.exports.ttl_minutes),
    )
    async with svc.sm() as s, s.begin():
        # file xuất chứa dữ liệu cá nhân: dọn bản hết hạn ngay, không đợi job đêm
        await s.execute(delete(Export).where(Export.expires_at <= now))
        s.add(e)
        await s.flush()
        s.expunge(e)
    return e


async def load_owned(svc: Services, export_id: str, ctx: UserContext) -> Export | None:
    """Sai chủ hoặc hết hạn đều trả None (không lộ là file có tồn tại)."""
    async with svc.sm() as s:
        e = await s.get(Export, export_id[:32])
        if e is None or e.owner_session != ctx.session_id or e.expires_at <= clock.now():
            return None
        s.expunge(e)
        return e
