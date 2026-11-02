"""Hook đặc thù của từng loại đơn (TK §7.6): gắn với form qua trường `hook` trong schema.

Mỗi hook khai:
- `prepare`: chạy ở endpoint xác nhận, TRƯỚC khi xếp hàng (vd sinh `ma_phieu` để trả ngay cho user);
- `precheck`: kiểm tra lại dữ liệu sống trong worker, trước step ghi đầu tiên (vd thiết bị còn đủ không);
- `steps`: các step ghi thêm sau xử lý chung (upload + dòng FormSubmissions), mỗi step idempotent;
- `message`: câu báo kết quả cho người dùng.
Đơn tự sinh schema (P4) không có hook: chỉ có xử lý chung. Agent không tự suy ra thao tác ghi đặc thù.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.models import Draft
    from app.forms.schema import FormSchema
    from app.forms.validators import VContext
    from app.outbox.steps import StepContext, StepFn
    from app.services import Services

PrepareFn = Callable[["Services", "FormSchema", "Draft", "VContext"], Awaitable[dict[str, Any]]]
PrecheckFn = Callable[["StepContext", "FormSchema", "Draft"], Awaitable[None]]
MessageFn = Callable[["FormSchema", "Draft", dict[str, Any]], str]


@dataclass
class Hook:
    name: str
    prepare: PrepareFn | None = None
    precheck: PrecheckFn | None = None
    steps: list[tuple[str, StepFn]] = field(default_factory=list)
    message: MessageFn | None = None
    # đơn này lưu link folder (thay vì link file) vào cột link_file
    link_folder: bool = False
    status: str = "DA_NOP"  # trang_thai ban đầu trên FormSubmissions


HOOKS: dict[str, Hook] = {}


def register_hook(h: Hook) -> Hook:
    HOOKS[h.name] = h
    return h
