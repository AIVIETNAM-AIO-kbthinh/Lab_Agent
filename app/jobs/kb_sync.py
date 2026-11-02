"""kb_sync (mỗi 15 phút): poll Drive changes, re-index kb/; kb_reconcile (CN 23:00): đối chiếu toàn bộ kb/."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import DriveState, IndexDoc
from app.google.drive import DriveFile
from app.jobs.base import JobContext, job
from app.logging import get_logger
from app.rag.drive_source import SUPPORTED, KbFile, classify_path, drive_doc_id, to_source_doc, walk_kb

log = get_logger(__name__)
TOKEN_KEY = "drive_changes_page_token"  # noqa: S105


async def _get_token(ctx: JobContext) -> str | None:
    async with ctx.svc.sm() as s:
        row = await s.get(DriveState, TOKEN_KEY)
        return row.value if row else None


async def _set_token(ctx: JobContext, token: str) -> None:
    async with ctx.svc.sm() as s, s.begin():
        row = await s.get(DriveState, TOKEN_KEY)
        if row is None:
            s.add(DriveState(key=TOKEN_KEY, value=token))
        else:
            row.value = token


async def _indexed(ctx: JobContext, doc_id: str) -> IndexDoc | None:
    async with ctx.svc.sm() as s:
        return await s.get(IndexDoc, doc_id)


async def sync_file(ctx: JobContext, f: DriveFile | None, file_id: str, removed: bool) -> str:
    """Trả hành động: indexed | unchanged | deleted | skipped | template."""
    svc = ctx.svc
    doc_id = drive_doc_id(file_id)
    if removed or f is None or f.trashed:
        if await _indexed(ctx, doc_id):
            if not ctx.dry_run:
                await svc.indexer.delete_doc(doc_id)
            return "deleted"
        return "skipped"
    if f.is_folder:
        return "skipped"
    path = await svc.paths.path_of(f)
    if path is None:
        return "skipped"
    if path.startswith("templates/") and not path.startswith("templates/_"):
        log.info("template_change_detected", path=path)  # P4: sinh schema
        return "template"
    access = classify_path(path, svc.config.index)
    if access is None or f.mime_type not in SUPPORTED:
        # file bị chuyển ra khỏi kb/ (vd vào _archive/) ⇒ xoá chunk
        if await _indexed(ctx, doc_id):
            if not ctx.dry_run:
                await svc.indexer.delete_doc(doc_id)
            return "deleted"
        return "skipped"
    existing = await _indexed(ctx, doc_id)
    if existing and existing.source_version == (f.version or f.modified_time) and existing.location == path:
        return "unchanged"
    if ctx.dry_run:
        return "indexed"
    doc = await to_source_doc(svc.google.drive, KbFile(f, path, access))
    if doc is None:
        return "skipped"
    outcome = await svc.indexer.upsert_doc(doc)
    return "indexed" if outcome.status == "indexed" else outcome.status


@job("kb_sync")
async def kb_sync(ctx: JobContext) -> dict[str, Any]:
    svc = ctx.svc
    token = await _get_token(ctx)
    if token is None:
        start = await svc.google.drive.start_page_token()
        if not ctx.dry_run:
            await _set_token(ctx, start)
            rec = await kb_reconcile(ctx)
            return {"status": "initialized", "page_token": start, "reconcile": rec}
        return {"status": "would_initialize"}
    counts: dict[str, int] = {}
    page: str | None = token
    new_start: str | None = None
    while page:
        changes, next_page, new_start_tok = await svc.google.drive.list_changes(page)
        for c in changes:
            try:
                action = await sync_file(ctx, c.file, c.file_id, c.removed)
            except Exception as e:
                log.error(
                    "kb_sync_file_failed", file_id=c.file_id, error=f"{type(e).__name__}: {str(e)[:200]}"
                )
                action = "error"
            counts[action] = counts.get(action, 0) + 1
        page = next_page
        new_start = new_start_tok or new_start
    if new_start and not ctx.dry_run and not counts.get("error"):
        await _set_token(ctx, new_start)
    if (counts.get("indexed") or counts.get("deleted")) and not ctx.dry_run:
        await svc.audit.log("system", "system", "kb_sync", "kb/", True, counts)
    return {"status": "ok", **counts}


@job("kb_reconcile")
async def kb_reconcile(ctx: JobContext) -> dict[str, Any]:
    svc = ctx.svc
    files = await walk_kb(svc.google.drive, svc.paths, svc.config.index)
    known = await svc.indexer.known("drive")
    live: set[str] = set()
    counts = {"indexed": 0, "unchanged": 0, "deleted": 0, "skipped": 0, "error": 0}
    # force=1: đọc lại mọi file (vd sau khi đổi cách chia chunk); indexer vẫn bỏ qua nếu hash không đổi
    force = str(ctx.params.get("force", "")).lower() in {"1", "true", "yes"}
    for kb in files:
        doc_id = drive_doc_id(kb.file.id)
        live.add(doc_id)
        row = known.get(doc_id)
        if (
            not force
            and row
            and row.source_version == (kb.file.version or kb.file.modified_time)
            and row.location == kb.path
        ):
            counts["unchanged"] += 1
            continue
        if ctx.dry_run:
            counts["indexed"] += 1
            continue
        try:
            doc = await to_source_doc(svc.google.drive, kb)
            if doc is None:
                counts["skipped"] += 1
                continue
            outcome = await svc.indexer.upsert_doc(doc)
            counts["indexed" if outcome.status == "indexed" else "unchanged"] += 1
        except Exception as e:
            counts["error"] += 1
            log.error("kb_reconcile_file_failed", path=kb.path, error=f"{type(e).__name__}: {str(e)[:200]}")
    stale = [d for d in known if d not in live]
    if not ctx.dry_run and not counts["error"]:
        for d in stale:
            await svc.indexer.delete_doc(d)
    counts["deleted"] = len(stale)
    if not ctx.dry_run and (counts["indexed"] or counts["deleted"]):
        await svc.audit.log("system", "system", "kb_reconcile", "kb/", True, counts)
    async with svc.sm() as s:
        total = len((await s.scalars(select(IndexDoc.doc_id).where(IndexDoc.source == "drive"))).all())
    return {**counts, "files": len(files), "indexed_docs": total, "dry_run": ctx.dry_run}
