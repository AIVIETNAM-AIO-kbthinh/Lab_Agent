"""draft_cleanup (03:00) và data_retention (hằng ngày): xoá bản nháp/upload quá hạn, hội thoại > N ngày."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app import clock
from app.auth import ratelimit, sessions
from app.db.models import Conversation, Draft, Export, PendingAction, TemplateDraft, Upload
from app.jobs.base import JobContext, job


@job("draft_cleanup")
async def draft_cleanup(ctx: JobContext) -> dict[str, Any]:
    now = clock.now()
    async with ctx.svc.sm() as s, s.begin():
        drafts = int(
            await s.scalar(select(func.count()).select_from(Draft).where(Draft.expires_at <= now)) or 0
        )
        actions = int(
            await s.scalar(
                select(func.count()).select_from(PendingAction).where(PendingAction.expires_at <= now)
            )
            or 0
        )
        # bản duyệt mẫu đơn: chỉ xoá bản chưa kích hoạt (bản ACTIVATED giữ làm lịch sử, đã bỏ bytes xem trước)
        stale_tpl = TemplateDraft.expires_at <= now, TemplateDraft.status != "ACTIVATED"
        templates = int(
            await s.scalar(select(func.count()).select_from(TemplateDraft).where(*stale_tpl)) or 0
        )
        exports = int(
            await s.scalar(select(func.count()).select_from(Export).where(Export.expires_at <= now)) or 0
        )
        if not ctx.dry_run:
            await s.execute(delete(Draft).where(Draft.expires_at <= now))
            await s.execute(delete(Export).where(Export.expires_at <= now))
            await s.execute(delete(PendingAction).where(PendingAction.expires_at <= now))
            await s.execute(delete(TemplateDraft).where(*stale_tpl))
    return {
        "drafts_deleted": drafts,
        "pending_actions_deleted": actions,
        "template_drafts_deleted": templates,
        "exports_deleted": exports,
        "dry_run": ctx.dry_run,
    }


@job("data_retention")
async def data_retention(ctx: JobContext) -> dict[str, Any]:
    cfg = ctx.svc.config.retention
    now = clock.now()
    cutoff = now - timedelta(days=cfg.conversation_days)
    files_removed = 0
    async with ctx.svc.sm() as s, s.begin():
        convs = int(
            await s.scalar(
                select(func.count()).select_from(Conversation).where(Conversation.updated_at < cutoff)
            )
            or 0
        )
        uploads = (await s.scalars(select(Upload).where(Upload.expires_at <= now))).all()
        if not ctx.dry_run:
            await s.execute(delete(Conversation).where(Conversation.updated_at < cutoff))  # messages cascade
            for u in uploads:
                p = Path(u.path)
                if p.is_file():
                    p.unlink(missing_ok=True)
                    files_removed += 1
                await s.delete(u)
            expired_sessions = await sessions.purge_expired(s)
            await ratelimit.purge(s)
        else:
            expired_sessions = 0
    return {
        "conversations_deleted": convs,
        "uploads_deleted": len(uploads),
        "files_removed": files_removed,
        "sessions_purged": expired_sessions,
        "dry_run": ctx.dry_run,
    }
