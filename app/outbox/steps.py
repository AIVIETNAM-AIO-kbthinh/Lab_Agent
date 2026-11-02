"""Định nghĩa loại op: danh sách step có tên, mỗi step idempotent theo (op_id, step)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.db.models import OutboxOp

if TYPE_CHECKING:
    from app.services import Services


@dataclass
class StepContext:
    svc: Services
    op_id: str
    kind: str
    payload: dict[str, Any]
    actor: str
    results: dict[str, Any] = field(default_factory=dict)  # step trước → result_json


StepFn = Callable[[StepContext], Awaitable[dict[str, Any] | None]]


@dataclass
class OpKind:
    name: str
    steps: list[tuple[str, StepFn]]
    # kiểm tra lại dữ liệu sống ngay trong worker, trước step ghi đầu tiên; ném PreconditionFailed để dừng op
    precheck: StepFn | None = None
    max_attempts: int | None = None


class PreconditionFailed(Exception):
    """Dữ liệu sống không còn thoả (vd thiết bị vừa bị mượn hết): op FAILED ngay, không retry."""


OPS: dict[str, OpKind] = {}


def register(kind: OpKind) -> OpKind:
    OPS[kind.name] = kind
    return kind


async def enqueue(
    s: AsyncSession, kind: str, payload: dict[str, Any], *, actor: str = "system", op_id: str | None = None
) -> str:
    """Xếp op vào hàng đợi. `op_id` cố định ⇒ enqueue lại không tạo op trùng."""
    if kind not in OPS:
        raise KeyError(f"Loại op chưa đăng ký: {kind}")
    values: dict[str, Any] = {
        "kind": kind,
        "payload_json": payload,
        "actor": actor,
        "status": "PENDING",
        "next_attempt_at": clock.now(),  # cùng nguồn thời gian với worker (tiêm được khi test)
    }
    if op_id:
        values["id"] = op_id
        await s.execute(insert(OutboxOp).values(**values).on_conflict_do_nothing(index_elements=["id"]))
        return op_id
    op = OutboxOp(**values)
    s.add(op)
    await s.flush()
    return op.id
