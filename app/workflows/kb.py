"""Thư ký / leader / admin đẩy tài liệu vào kho kiến thức (authentication_plan.md C4).

File người dùng gửi trong chat (upload của đúng session) → chọn `public` (ai cũng đọc được, kể cả khách) hoặc
`internal` (chỉ thành viên) → bản tóm tắt + nút Xác nhận → lưu lên kb/<thư mục>/ trên Drive + index ngay.
Không cần duyệt thêm. Index lỗi không làm hỏng việc lưu: job kb_sync sẽ index lại khi thấy file mới.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app import clock
from app.auth.roles import UserContext
from app.db.models import Upload
from app.domain.names import ascii_key
from app.errors import ValidationFailed
from app.logging import get_logger
from app.outbox.steps import OpKind, PreconditionFailed, StepContext, register
from app.workflows.actions import finish, propose

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)
LABELS = {"public": "Công khai (khách cũng đọc được)", "internal": "Nội bộ (chỉ thành viên lab)"}


def _clean_subfolder(sub: str | None) -> str:
    """Thư mục con tuỳ chọn, vd 'noi-quy'. Chỉ chữ/số/gạch, tối đa 2 cấp; không bắt đầu bằng '_' (bị bỏ qua khi index)."""
    if not sub:
        return ""
    parts = []
    for p in sub.strip("/").split("/")[:2]:
        p = re.sub(r"[^a-z0-9-]+", "-", ascii_key(p)).strip("-")
        if p:
            parts.append(p)
    return "/".join(parts)


def _filename(title: str | None, up: Upload) -> str:
    base = (title or Path(up.filename).stem or "TaiLieu").strip()
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", base).strip(" ._")[:120] or "TaiLieu"
    return f"{base}.{up.ext}"


async def _upload(svc: Services, upload_id: str, session_id: str) -> Upload:
    async with svc.sm() as s:
        up = await s.get(Upload, upload_id)
    if up is None or up.session_public_id != session_id or up.expires_at <= clock.now():
        raise ValidationFailed(
            user_message="Không tìm thấy file đã tải lên (hoặc file đã hết hạn), hãy gửi lại."
        )
    return up


async def propose_publish(
    svc: Services,
    ctx: UserContext,
    upload_id: str,
    folder: str,
    title: str | None,
    subfolder: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if folder not in LABELS:
        raise ValidationFailed(user_message="Thư mục phải là public hoặc internal.")
    up = await _upload(svc, upload_id, ctx.session_id)
    base = svc.config.index.kb_public_path if folder == "public" else svc.config.index.kb_internal_path
    sub = _clean_subfolder(subfolder)
    folder_path = f"{base.strip('/')}/{sub}" if sub else base.strip("/")
    name = _filename(title, up)
    summary = [
        f"Đưa '{name}' ({up.size // 1024 or 1} KB) vào {folder_path}/",
        f"Phạm vi: {LABELS[folder]}",
        "Tài liệu có hiệu lực ngay sau khi xác nhận (trợ lý dùng để trả lời), không cần duyệt thêm.",
    ]
    payload = {
        "upload_id": up.id,
        "folder": folder,
        "folder_path": folder_path,
        "name": name,
        "mime": up.mime,
    }
    return await propose(svc, ctx, "kb_publish", payload, summary)


async def _precheck(ctx: StepContext) -> dict[str, Any] | None:
    async with ctx.svc.sm() as s:
        up = await s.get(Upload, ctx.payload["upload_id"])
    if up is None or not Path(up.path).exists():
        raise PreconditionFailed("File đã tải lên không còn (đã hết hạn), hãy gửi lại.")
    return None


async def step_upload(ctx: StepContext) -> dict[str, Any]:
    svc, p = ctx.svc, ctx.payload
    folder = await svc.paths.ensure_folder(p["folder_path"])
    # chạy lại không tạo file trùng: tìm file đã tải lên bởi đúng yêu cầu này
    for f in await svc.google.drive.list_children(folder.id):
        if f.app_properties.get("labagent_kb_action") == p["action_id"] and not f.trashed:
            return {"file_id": f.id, "name": f.name, "link": f.link}
    async with svc.sm() as s:
        up = await s.get(Upload, p["upload_id"])
    assert up is not None
    f = await svc.google.drive.upload(
        p["name"],
        folder.id,
        Path(up.path).read_bytes(),
        p["mime"],
        app_properties={"labagent_kb_action": p["action_id"]},
    )
    return {"file_id": f.id, "name": f.name, "link": f.link}


async def step_index(ctx: StepContext) -> dict[str, Any]:
    from app.rag.drive_source import KbFile, classify_path, to_source_doc

    svc = ctx.svc
    up = ctx.results["upload"]
    path = f"{ctx.payload['folder_path']}/{up['name']}"
    access = classify_path(path, svc.config.index)
    if access is None:
        return {"indexed": False, "reason": "ngoài kb/"}
    try:
        f = await svc.google.drive.get_file(up["file_id"])
        doc = await to_source_doc(svc.google.drive, KbFile(f, path, access))
        if doc is None:
            return {"indexed": False, "reason": "không trích được nội dung"}
        out = await svc.indexer.upsert_doc(doc, force=True)
        return {"indexed": True, "chunks": getattr(out, "chunks", None)}
    except Exception as e:
        log.warning("kb_publish_index_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"indexed": False, "reason": type(e).__name__}


async def step_audit(ctx: StepContext) -> None:
    p, up = ctx.payload, ctx.results["upload"]
    await ctx.svc.audit.log(
        ctx.actor,
        p.get("requested_role", "thu_ki"),
        "kb_publish",
        f"{p['folder_path']}/{up['name']}",
        True,
        {"link": up["link"], "index": ctx.results.get("index")},
    )


async def step_finish(ctx: StepContext) -> dict[str, Any]:
    p, up = ctx.payload, ctx.results["upload"]
    idx = ctx.results.get("index") or {}
    tail = (
        " Trợ lý đã dùng được tài liệu này."
        if idx.get("indexed")
        else " Tài liệu sẽ được đưa vào tìm kiếm trong lần đồng bộ kế tiếp (tối đa ~15 phút)."
    )
    return await finish(
        ctx.svc,
        p["action_id"],
        f"Đã lưu '{up['name']}' vào {p['folder_path']}/: {up['link']}.{tail}",
        link=up["link"],
    )


register(
    OpKind(
        "action:kb_publish",
        [("upload", step_upload), ("index", step_index), ("audit", step_audit), ("finish", step_finish)],
        precheck=_precheck,
    )
)
