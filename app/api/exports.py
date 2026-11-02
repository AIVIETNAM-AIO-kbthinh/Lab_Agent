"""GET /exports/{id}: tải file xuất theo yêu cầu. Chỉ đúng session đã yêu cầu, trong thời hạn."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.api.forms import NO_STORE, _disposition
from app.auth.deps import current_user, get_services
from app.exports import store

router = APIRouter(tags=["exports"])


@router.get("/exports/{export_id}")
async def download_export(export_id: str, request: Request) -> Response:
    svc = get_services(request)
    ctx = await current_user(request)
    e = await store.load_owned(svc, export_id, ctx)
    if e is None:
        raise HTTPException(status_code=404, detail="File không còn hiệu lực, hãy yêu cầu trợ lý xuất lại.")
    return Response(
        e.data,
        media_type=e.mime,
        headers={**NO_STORE, "Content-Disposition": _disposition("attachment", e.filename)},
    )
