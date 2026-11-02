"""POST /chat: stream câu trả lời qua SSE (text + sự kiện ui_action)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.loop import AgentRunner
from app.auth import ratelimit
from app.auth.deps import current_account, current_user, get_services, require_csrf
from app.auth.roles import UserContext
from app.config import auth_bypass_enabled
from app.errors import NotFound, RateLimited
from app.logging import get_logger

router = APIRouter(tags=["chat"])
log = get_logger(__name__)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = Field(None, max_length=32)


def sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


async def _limits(request: Request, ctx: UserContext) -> None:
    svc = get_services(request)
    if auth_bypass_enabled(svc.settings, svc.config):
        return  # chế độ dev cô lập auth: không giới hạn tần suất để test thoải mái
    sec = svc.config.security
    async with svc.sm() as s, s.begin():
        if ctx.is_member_plus:
            await ratelimit.hit(
                s, f"chat:acct:{ctx.account_id or ctx.session_id}", sec.member_msgs_per_hour, 3600
            )
        else:
            await ratelimit.hit(s, f"chat:sess:{ctx.session_id}", sec.guest_msgs_per_session_hour, 3600)
            await ratelimit.hit(s, f"chat:ip:{ctx.ip}", sec.guest_msgs_per_ip_hour, 3600)


@router.post("/chat", dependencies=[Depends(require_csrf)])
async def chat(body: ChatIn, request: Request) -> StreamingResponse:
    svc = get_services(request)
    ctx = await current_user(request)
    acc = await current_account(request)
    if acc is not None and acc.must_change_password:
        raise HTTPException(status_code=403, detail="Bạn cần đổi mật khẩu trước khi dùng chatbot.")
    try:
        await _limits(request, ctx)
    except RateLimited as e:
        raise HTTPException(status_code=429, detail=e.user_message) from None
    runner = AgentRunner(svc)
    rid = getattr(request.state, "request_id", None)

    async def gen() -> AsyncIterator[str]:
        try:
            async for ev in runner.run_turn(ctx, body.message, body.conversation_id, request_id=rid):
                yield sse(ev)
        except NotFound:
            yield sse(
                {"type": "error", "message": "Hội thoại không tồn tại, vui lòng bắt đầu hội thoại mới."}
            )
        except Exception as e:
            log.exception("chat_stream_failed", error_type=type(e).__name__)
            yield sse({"type": "error", "message": "Đã có lỗi xảy ra, vui lòng thử lại sau."})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/{conversation_id}/history")
async def history(conversation_id: str, request: Request) -> dict[str, Any]:
    svc = get_services(request)
    ctx = await current_user(request)
    runner = AgentRunner(svc)
    try:
        return {"messages": await runner.conversations.history_for_ui(conversation_id, ctx)}
    except NotFound:
        raise HTTPException(status_code=404, detail="Không tìm thấy hội thoại.") from None
