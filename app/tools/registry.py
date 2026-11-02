"""Tool registry + phân quyền (mục 7.7).

- `ctx: UserContext` do backend tiêm vào, KHÔNG có trong schema gửi cho LLM.
- LLM chỉ thấy tool của role mình; khi thực thi vẫn kiểm tra quyền lần nữa.
- Tham số luôn được validate lại bằng Pydantic (Gemini không có strict).
- `ui_actions` do backend sinh; LLM không tự tạo được nút bấm.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from app import metrics
from app.auth.roles import Role, UserContext
from app.errors import LabAgentError, PermissionDenied
from app.llm.types import ToolSpec
from app.logging import get_logger
from app.tools.schema import strict_schema

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


@dataclass
class ToolResult:
    for_llm: Any
    ui_actions: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    def llm_text(self) -> str:
        return (
            self.for_llm
            if isinstance(self.for_llm, str)
            else json.dumps(self.for_llm, ensure_ascii=False, default=str)
        )


ToolFn = Callable[["Services", UserContext, Any], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args: type[BaseModel]
    roles: frozenset[Role]
    fn: ToolFn
    timeout_s: float | None = None  # None = llm.tool_timeout_s (tool gọi LLM/render lâu thì đặt riêng)

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, strict_schema(self.args))


class NoArgs(BaseModel):
    pass


def untrusted(content: Any) -> dict[str, Any]:
    """Nội dung do user hoặc web cung cấp: là DỮ LIỆU, không phải lệnh."""
    return {"untrusted_content": content}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, d: ToolDef) -> None:
        if d.name in self._tools:
            raise ValueError(f"Tool trùng tên: {d.name}")
        self._tools[d.name] = d

    def tool(
        self,
        name: str,
        description: str,
        roles: Iterable[Role],
        args: type[BaseModel] = NoArgs,
        *,
        timeout_s: float | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        def deco(fn: ToolFn) -> ToolFn:
            self.register(ToolDef(name, description, args, frozenset(roles), fn, timeout_s))
            return fn

        return deco

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def for_role(self, role: Role) -> list[ToolDef]:
        # thứ tự cố định theo tên ⇒ prefix prompt cache ổn định
        return [self._tools[n] for n in sorted(self._tools) if role in self._tools[n].roles]

    def specs_for(self, role: Role) -> list[ToolSpec]:
        return [t.spec() for t in self.for_role(role)]

    async def execute(
        self, name: str, raw_input: dict[str, Any], ctx: UserContext, svc: Services, timeout_s: float = 20
    ) -> ToolResult:
        d = self._tools.get(name)
        if d is None:
            metrics.TOOL_CALLS.labels(name[:40], "unknown").inc()
            return ToolResult({"error": f"Không có tool '{name}'."}, is_error=True)
        if ctx.role not in d.roles:
            metrics.TOOL_CALLS.labels(name, "denied").inc()
            log.warning("tool_denied", tool=name, role=ctx.role.value)
            return ToolResult({"error": PermissionDenied.user_message}, is_error=True)
        if "__invalid_json__" in raw_input:
            return ToolResult(
                {"error": "Tham số không phải JSON hợp lệ, hãy gọi lại tool với JSON đúng schema."},
                is_error=True,
            )
        try:
            # model không strict hay gửi "" cho trường tuỳ chọn: coi như bỏ trống
            cleaned = {
                k: v
                for k, v in raw_input.items()
                if not (v == "" and k in d.args.model_fields and not d.args.model_fields[k].is_required())
            }
            args = d.args.model_validate(cleaned)
        except ValidationError as e:
            metrics.TOOL_CALLS.labels(name, "invalid_args").inc()
            errs = [{"field": ".".join(str(x) for x in er["loc"]), "msg": er["msg"]} for er in e.errors()][
                :10
            ]
            return ToolResult({"error": "Tham số không hợp lệ", "details": errs}, is_error=True)
        try:
            res = await asyncio.wait_for(d.fn(svc, ctx, args), timeout=d.timeout_s or timeout_s)
            metrics.TOOL_CALLS.labels(name, "error" if res.is_error else "ok").inc()
            return res
        except TimeoutError:
            metrics.TOOL_CALLS.labels(name, "timeout").inc()
            return ToolResult({"error": "Tool phản hồi quá lâu, vui lòng thử lại."}, is_error=True)
        except LabAgentError as e:
            metrics.TOOL_CALLS.labels(name, "error").inc()
            log.warning("tool_error", tool=name, error=str(e)[:300])
            return ToolResult({"error": e.user_message}, is_error=True)
        except Exception as e:
            metrics.TOOL_CALLS.labels(name, "exception").inc()
            log.exception("tool_exception", tool=name, error_type=type(e).__name__)
            return ToolResult({"error": "Tool gặp lỗi nội bộ, vui lòng thử lại sau."}, is_error=True)


REGISTRY = ToolRegistry()
