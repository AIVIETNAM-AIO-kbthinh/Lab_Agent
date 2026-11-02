"""Endpoint duyệt mẫu đơn mới (P4) — gọi từ NÚT BẤM trên thẻ xem trước, không phải tool của LLM.

Leader và admin (authentication_plan.md C5), và chỉ đúng session đã tạo bản duyệt. Danh sách mẫu đơn + ngừng dùng
cho trang quản trị.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import clock
from app.api.forms import NO_STORE, _disposition
from app.auth.deps import get_services, require_csrf, require_leader_admin
from app.auth.roles import UserContext
from app.db.models import FormVersion, TemplateDraft
from app.errors import DraftChanged, DraftInvalid
from app.forms.schema_gen import activate as _activate  # noqa: F401  (đăng ký op outbox)
from app.forms.schema_gen.review import TemplateReview
from app.google.drive import DOCX, PDF

router = APIRouter(
    prefix="/admin/api/templates", tags=["templates"], dependencies=[Depends(require_leader_admin)]
)


async def _owned(request: Request, td_id: str, admin: UserContext) -> TemplateDraft:
    try:
        return await TemplateReview(get_services(request)).owned(td_id, admin, allow_closed=True)
    except DraftInvalid as e:
        raise HTTPException(status_code=404, detail=e.user_message) from None


@router.get("/{td_id}/preview")
async def preview(
    td_id: str, request: Request, admin: UserContext = Depends(require_leader_admin)
) -> Response:
    td = await _owned(request, td_id, admin)
    if not td.preview_bytes:
        raise HTTPException(status_code=404, detail="Bản điền thử không còn hiệu lực.")
    mime = td.preview_mime or PDF
    ext = "pdf" if mime == PDF else "docx"
    headers = {
        **NO_STORE,
        "Content-Disposition": _disposition(
            "inline" if ext == "pdf" else "attachment", f"{td.form_id}_dien_thu_v{td.preview_version}.{ext}"
        ),
    }
    return Response(td.preview_bytes, media_type=mime, headers=headers)


@router.get("/{td_id}/blank")
async def blank(td_id: str, request: Request, admin: UserContext = Depends(require_leader_admin)) -> Response:
    td = await _owned(request, td_id, admin)
    name = td.schema_json["template"]["drive_path"].rsplit("/", 1)[-1]
    return Response(
        td.blank_docx,
        media_type=DOCX,
        headers={**NO_STORE, "Content-Disposition": _disposition("attachment", name)},
    )


class ConfirmIn(BaseModel):
    shown_hash: str = Field(min_length=64, max_length=64)


@router.post("/{td_id}/confirm", dependencies=[Depends(require_csrf)])
async def confirm(
    td_id: str, body: ConfirmIn, request: Request, admin: UserContext = Depends(require_leader_admin)
) -> dict[str, Any]:
    try:
        r = await TemplateReview(get_services(request)).confirm(td_id, body.shown_hash, admin)
    except DraftChanged as e:
        raise HTTPException(status_code=409, detail=e.user_message) from None
    except DraftInvalid as e:
        raise HTTPException(status_code=410, detail=e.user_message) from None
    return {"ok": r.ok, "status": r.status, "message": r.message, "result": r.data}


@router.post("/{td_id}/cancel", dependencies=[Depends(require_csrf)])
async def cancel(
    td_id: str, request: Request, admin: UserContext = Depends(require_leader_admin)
) -> dict[str, Any]:
    try:
        r = await TemplateReview(get_services(request)).cancel(td_id, admin)
    except DraftInvalid as e:
        raise HTTPException(status_code=410, detail=e.user_message) from None
    return {"ok": True, **r.data}


@router.get("")
async def list_templates(request: Request) -> dict[str, Any]:
    """Mẫu đơn đang dùng / đã ngừng + bản duyệt đang mở (trang admin)."""
    svc = get_services(request)
    async with svc.sm() as s:
        versions = (
            await s.scalars(select(FormVersion).order_by(FormVersion.form_id, FormVersion.version))
        ).all()
        drafts = (
            await s.scalars(
                select(TemplateDraft)
                .where(TemplateDraft.status == "PREVIEWED", TemplateDraft.expires_at > clock.now())
                .order_by(TemplateDraft.created_at.desc())
                .limit(50)
            )
        ).all()
    return {
        "forms": [
            {
                "form_id": v.form_id,
                "version": v.version,
                "status": v.status,
                "title": v.title,
                "roles": v.roles,
                "hook": (v.schema_json or {}).get("hook"),
                "fields": len((v.schema_json or {}).get("fields", [])),
                "updated_at": v.updated_at,
            }
            for v in versions
        ],
        "pending": [
            {
                "id": d.id,
                "form_id": d.form_id,
                "title": d.schema_json.get("title"),
                "created_at": d.created_at,
            }
            for d in drafts
        ],
    }


@router.post("/forms/{form_id}/deactivate", dependencies=[Depends(require_csrf)])
async def deactivate(
    form_id: str, request: Request, admin: UserContext = Depends(require_leader_admin)
) -> dict[str, Any]:
    svc = get_services(request)
    n = await svc.forms.deactivate(form_id)
    if not n:
        raise HTTPException(status_code=404, detail="Không có mẫu đơn đang dùng với mã này.")
    await svc.audit.log(admin.actor, admin.role.value, "template_deactivate", form_id, True)
    return {"ok": True, "deactivated": n}
