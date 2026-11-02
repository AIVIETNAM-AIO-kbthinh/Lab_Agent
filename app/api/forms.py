"""Endpoint form engine + yêu cầu chờ xác nhận. Gọi từ NÚT BẤM trên UI, không phải tool của LLM (TK §7.5)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth.deps import current_user, get_services, require_csrf
from app.errors import DraftChanged, DraftInvalid, LabAgentError, NotFound
from app.forms.engine import FormEngine
from app.forms.links import verify_blank
from app.google.drive import DOCX
from app.workflows import actions

router = APIRouter(tags=["forms"])
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _disposition(kind: str, filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "file"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/drafts/{draft_id}/preview")
async def preview_file(draft_id: str, request: Request) -> Response:
    svc = get_services(request)
    ctx = await current_user(request)
    eng = FormEngine(svc)
    try:
        d = await eng.drafts.owned(draft_id, ctx, allow_closed=True)
    except DraftInvalid:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản xem trước.") from None
    data = await eng.shown_bytes(d)
    if not data:
        raise HTTPException(status_code=404, detail="Bản xem trước không còn hiệu lực, hãy yêu cầu bản mới.")
    if d.mode == "upload":
        mime = "application/pdf" if data.startswith(b"%PDF-") else DOCX
    else:
        mime = d.preview_mime or "application/pdf"
    ext = "pdf" if mime == "application/pdf" else "docx"
    kind = "inline" if ext == "pdf" else "attachment"
    headers = {
        **NO_STORE,
        "Content-Disposition": _disposition(kind, f"{d.form_id}_v{d.preview_version}.{ext}"),
    }
    return Response(data, media_type=mime, headers=headers)


class ConfirmIn(BaseModel):
    shown_hash: str = Field(min_length=64, max_length=64)


@router.post("/drafts/{draft_id}/confirm", dependencies=[Depends(require_csrf)])
async def confirm_draft(draft_id: str, body: ConfirmIn, request: Request) -> dict[str, Any]:
    svc = get_services(request)
    ctx = await current_user(request)
    try:
        r = await FormEngine(svc).confirm(draft_id, body.shown_hash, ctx)
    except DraftChanged as e:
        raise HTTPException(status_code=409, detail=e.user_message) from None
    except DraftInvalid as e:
        raise HTTPException(status_code=410, detail=e.user_message) from None
    return {"ok": r.ok, "status": r.status, "message": r.message, "result": r.data, "errors": r.issues}


@router.post("/drafts/{draft_id}/cancel", dependencies=[Depends(require_csrf)])
async def cancel_draft(draft_id: str, request: Request) -> dict[str, Any]:
    svc = get_services(request)
    ctx = await current_user(request)
    try:
        r = await FormEngine(svc).cancel(draft_id, ctx)
    except DraftInvalid as e:
        raise HTTPException(status_code=410, detail=e.user_message) from None
    return {"ok": True, **r.data}


@router.get("/drafts/{draft_id}/status")
async def draft_status(draft_id: str, request: Request) -> dict[str, Any]:
    svc = get_services(request)
    ctx = await current_user(request)
    eng = FormEngine(svc)
    try:
        d = await eng.drafts.owned(draft_id, ctx, allow_closed=True)
    except DraftInvalid as e:
        raise HTTPException(status_code=404, detail=e.user_message) from None
    if d.status in ("CONFIRMING", "SUBMITTED"):
        r = await eng._result_of(d)
        return {"status": r.status, "message": r.message, "result": r.data}
    return {"status": d.status}


@router.get("/forms/{form_id}/blank")
async def blank_form(form_id: str, exp: int, sig: str, request: Request) -> Response:
    svc = get_services(request)
    if not verify_blank(svc, form_id, exp, sig):
        raise HTTPException(status_code=403, detail="Link tải mẫu đơn đã hết hạn, hãy hỏi lại trợ lý.")
    fd = await svc.forms.active(form_id)
    if fd is None:
        raise HTTPException(status_code=404, detail="Không có mẫu đơn này.")
    try:
        file_id = fd.schema.template.file_id or (await svc.paths.resolve(fd.schema.template.drive_path)).id
        data, _ = await svc.templates.blank(file_id)
    except (NotFound, LabAgentError):
        raise HTTPException(status_code=404, detail="Không tải được mẫu đơn, vui lòng thử lại sau.") from None
    name = fd.schema.template.drive_path.rsplit("/", 1)[-1].removesuffix(".docx") + ".docx"
    return Response(
        data, media_type=DOCX, headers={**NO_STORE, "Content-Disposition": _disposition("attachment", name)}
    )


@router.post("/actions/{action_id}/confirm", dependencies=[Depends(require_csrf)])
async def confirm_action(action_id: str, request: Request) -> dict[str, Any]:
    svc = get_services(request)
    ctx = await current_user(request)
    try:
        r = await actions.confirm(svc, action_id, ctx)
    except actions.ActionInvalid as e:
        raise HTTPException(status_code=410, detail=e.user_message) from None
    return {"ok": r.ok, "status": r.status, "message": r.message, "result": r.data}


@router.post("/actions/{action_id}/cancel", dependencies=[Depends(require_csrf)])
async def cancel_action(action_id: str, request: Request) -> dict[str, Any]:
    svc = get_services(request)
    ctx = await current_user(request)
    try:
        r = await actions.cancel(svc, action_id, ctx)
    except actions.ActionInvalid as e:
        raise HTTPException(status_code=410, detail=e.user_message) from None
    return {"ok": r.ok, "status": r.status, "message": r.message}
