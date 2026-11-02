"""Yêu cầu chờ xác nhận (pending_actions): tool chỉ TẠO yêu cầu + bản tóm tắt; người dùng bấm nút mới ghi.

Áp dụng cho trả / gia hạn thiết bị, thay đổi thành viên (duyệt đơn, cho rời lab, đổi role), đăng ký lịch trực,
đẩy tài liệu vào KB, sửa dữ liệu trong data/ (authentication_plan.md §4).
Xác nhận là endpoint backend (không phải tool LLM); khi xác nhận kiểm tra lại quyền theo role hiện tại.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import update

from app import clock
from app.auth.roles import (
    ADMIN_ONLY,
    BORROWERS,
    LEADER_ADMIN,
    MEMBER_PLUS,
    STAFF,
    Role,
    UserContext,
)
from app.db.models import OutboxOp, PendingAction
from app.errors import DraftInvalid, PermissionDenied
from app.logging import get_logger

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)

# Ai được TẠO và XÁC NHẬN từng loại yêu cầu (authentication_plan.md §4). Kiểm tra cả lúc tạo và lúc bấm Xác nhận.
ACTION_ROLES: dict[str, frozenset[Role]] = {
    "equipment_return": BORROWERS,
    "equipment_extend": BORROWERS,
    "member_approve": ADMIN_ONLY,
    "member_reject": ADMIN_ONLY,
    "member_remove": ADMIN_ONLY,
    "member_set_role": ADMIN_ONLY,
    "member_edit": ADMIN_ONLY,
    "duty_register": MEMBER_PLUS,
    "kb_publish": STAFF,
    "data_edit": LEADER_ADMIN,
}
TITLES = {
    "equipment_return": "Xác nhận trả thiết bị",
    "equipment_extend": "Xác nhận gia hạn mượn",
    "member_approve": "Xác nhận duyệt đơn gia nhập",
    "member_reject": "Xác nhận từ chối đơn gia nhập",
    "member_remove": "Xác nhận cho thành viên rời lab",
    "member_set_role": "Xác nhận đổi vai trò",
    "member_edit": "Xác nhận sửa thông tin thành viên",
    "duty_register": "Xác nhận đăng ký lịch trực",
    "kb_publish": "Xác nhận đưa tài liệu vào kho kiến thức",
    "data_edit": "Xác nhận sửa dữ liệu",
}


class ActionInvalid(DraftInvalid):
    user_message = "Yêu cầu này không còn hiệu lực, vui lòng tạo lại."


@dataclass
class ActionResult:
    ok: bool
    status: str  # DONE | PROCESSING | REJECTED | FAILED
    message: str
    data: dict[str, Any] = field(default_factory=dict)


async def propose(
    svc: Services, ctx: UserContext, kind: str, payload: dict[str, Any], summary: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Tạo yêu cầu. Trả (for_llm, ui_action)."""
    if ctx.role not in ACTION_ROLES[kind]:
        raise PermissionDenied(f"{ctx.role} không được {kind}")
    a = PendingAction(
        kind=kind,
        owner_session=ctx.session_id,
        payload_json={
            **payload,
            "requested_by": ctx.ho_ten or ctx.email or ctx.actor,
            "requested_role": ctx.role.value,
        },
        status="PENDING",
        expires_at=clock.now() + timedelta(hours=svc.config.drafts.pending_action_ttl_hours),
        summary="\n".join(summary),
        role=ctx.role.value,
        actor=ctx.actor,
    )
    async with svc.sm() as s, s.begin():
        s.add(a)
        await s.flush()
        aid = a.id
    ui = {"type": "action_confirm", "action_id": aid, "title": TITLES[kind], "summary": summary}
    data = {
        "action_id": aid,
        "status": "CHO_XAC_NHAN",
        "summary": summary,
        "next_step": "Đã hiện bản tóm tắt kèm nút Xác nhận/Huỷ. Nhắc người dùng kiểm tra và bấm Xác nhận; "
        "KHÔNG nói là đã thực hiện xong.",
    }
    return data, ui


async def _owned(svc: Services, action_id: str, ctx: UserContext) -> PendingAction:
    async with svc.sm() as s:
        a = await s.get(PendingAction, action_id)
        if a is not None:
            s.expunge(a)
    if a is None or a.owner_session != ctx.session_id:
        raise ActionInvalid(f"action {action_id}: không tồn tại/không thuộc session")
    return a


async def confirm(svc: Services, action_id: str, ctx: UserContext) -> ActionResult:
    from app.outbox.steps import enqueue
    from app.outbox.worker import wait_for

    a = await _owned(svc, action_id, ctx)
    if a.status in ("CONFIRMING", "DONE"):
        return await result_of(svc, a.id, already=True)
    if a.status != "PENDING" or a.expires_at <= clock.now():
        raise ActionInvalid(f"action {a.id}: {a.status}")
    if ctx.role not in ACTION_ROLES[a.kind]:
        raise PermissionDenied(f"{ctx.role} không còn quyền {a.kind}")
    async with svc.sm() as s, s.begin():
        res = await s.execute(
            update(PendingAction)
            .where(PendingAction.id == a.id, PendingAction.status == "PENDING")
            .values(status="CONFIRMING", op_id=a.id)
            .returning(PendingAction.id)
        )
        if res.scalar_one_or_none() is None:
            return await result_of(svc, a.id, already=True)
        await enqueue(s, f"action:{a.kind}", {"action_id": a.id, **a.payload_json}, actor=a.actor, op_id=a.id)
    log.info("action_confirmed", kind=a.kind, action_id=a.id)
    await wait_for(svc, a.id, svc.config.drafts.confirm_wait_s)
    return await result_of(svc, a.id)


async def result_of(svc: Services, action_id: str, *, already: bool = False) -> ActionResult:
    async with svc.sm() as s:
        a = await s.get(PendingAction, action_id)
        op = await s.get(OutboxOp, action_id)
    assert a is not None
    if a.status == "DONE":
        msg = (a.result_json or {}).get("message", "Đã thực hiện.")
        return ActionResult(
            True, "DONE", ("(Đã thực hiện trước đó) " if already else "") + msg, a.result_json or {}
        )
    if op is not None and op.status == "FAILED":
        err = op.last_error or ""
        if err.startswith("Precondition:"):
            await _set(svc, a.id, status="REJECTED")
            return ActionResult(False, "REJECTED", err.removeprefix("Precondition:").strip())
        await _set(svc, a.id, status="FAILED")
        return ActionResult(
            False, "FAILED", "Hệ thống chưa thực hiện được do lỗi kết nối. Admin đã được báo để xử lý."
        )
    return ActionResult(True, "PROCESSING", "Yêu cầu đã được ghi nhận và đang được xử lý.")


async def cancel(svc: Services, action_id: str, ctx: UserContext) -> ActionResult:
    a = await _owned(svc, action_id, ctx)
    if a.status != "PENDING":
        raise ActionInvalid(f"action {a.id}: {a.status}")
    await _set(svc, a.id, status="CANCELLED")
    return ActionResult(True, "CANCELLED", "Đã huỷ yêu cầu.")


async def _set(svc: Services, action_id: str, **values: Any) -> None:
    async with svc.sm() as s, s.begin():
        await s.execute(update(PendingAction).where(PendingAction.id == action_id).values(**values))


async def finish(svc: Services, action_id: str, message: str, **data: Any) -> dict[str, Any]:
    """Step cuối của mọi op action: đánh dấu DONE + lưu câu báo kết quả."""
    await _set(svc, action_id, status="DONE", result_json={"message": message, **data})
    return {"message": message}


MessageFn = Callable[[dict[str, Any]], str]
