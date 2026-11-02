"""Hook `meeting_minutes` — biên bản họp (TK §8.4).

Lưu dạng Google Doc vào kb/internal/meeting-minutes/ (tên theo NGÀY HỌP), re-index ngay với doc_date = ngày họp.
Index lỗi không làm hỏng việc lưu: job kb_sync sẽ index lại khi thấy file mới trên Drive.
"""

from __future__ import annotations

from typing import Any

from app.db.models import Draft
from app.forms.opctx import load_draft
from app.forms.schema import FormSchema
from app.hooks.base import Hook, register_hook
from app.logging import get_logger
from app.outbox.steps import StepContext

log = get_logger(__name__)


async def step_reindex(ctx: StepContext) -> dict[str, Any]:
    from app.rag.drive_source import KbFile, classify_path, to_source_doc

    svc = ctx.svc
    up = ctx.results["upload"]
    path = f"{up['folder_path']}/{up['name']}"
    access = classify_path(path, svc.config.index)
    if access is None:
        return {"indexed": False, "reason": "ngoài kb/"}
    try:
        f = await svc.google.drive.get_file(up["file_id"])
        doc = await to_source_doc(svc.google.drive, KbFile(f, path, access))
        if doc is None:
            return {"indexed": False, "reason": "không trích được nội dung"}
        out = await svc.indexer.upsert_doc(doc, force=True)
        return {"indexed": True, "chunks": getattr(out, "chunks", None), "doc_date": str(doc.doc_date or "")}
    except Exception as e:
        log.warning("minutes_reindex_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"indexed": False, "reason": f"{type(e).__name__}"}


def _message(schema: FormSchema, d: Draft, results: dict[str, Any]) -> str:
    ngay = (d.fields_json or {}).get("ngay_hop", "")
    return f"Biên bản họp ngày {ngay} đã được lưu vào tài liệu nội bộ: {results['upload']['link']}"


async def _precheck(ctx: StepContext, schema: FormSchema, d: Draft) -> None:
    await load_draft(ctx)


HOOK = register_hook(
    Hook("meeting_minutes", precheck=_precheck, steps=[("reindex", step_reindex)], message=_message)
)
