"""Vòng lặp tool-use tự viết, stream sự kiện cho SSE (mục 7.8).

Sự kiện: text · reset · tool · ui_action · done · error
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app import clock, metrics
from app.agents.conversation import ConversationStore
from app.agents.router import profile_for, system_prompt, user_header
from app.auth.roles import UserContext
from app.errors import BudgetExceeded, LabAgentError, LLMUnavailable
from app.llm.types import (
    Final,
    LLMError,
    LLMResponse,
    Reset,
    TextDelta,
    ToolCall,
    UsageContext,
    tool_result,
    tool_results_message,
    user_text,
)
from app.logging import get_logger
from app.tools.registry import ToolResult

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)
Event = dict[str, Any]

REFUSAL_ANSWER = (
    "Xin lỗi, mình không thể hỗ trợ yêu cầu này. Bạn có thể hỏi mình về các thông tin khác của lab."
)
MAX_TOKENS_NOTE = "\n\n_(Câu trả lời bị cắt do quá dài. Bạn có thể hỏi cụ thể hơn.)_"
UNFINISHED_ANSWER = "Mình chưa xử lý xong yêu cầu này. Bạn thử hỏi lại ngắn gọn hoặc tách thành từng câu nhé."


class AgentRunner:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.conversations = ConversationStore(svc.sm)

    async def _run_tools(self, calls: list[ToolCall], ctx: UserContext) -> list[tuple[ToolCall, ToolResult]]:
        timeout = self.svc.config.llm.tool_timeout_s

        async def one(c: ToolCall) -> tuple[ToolCall, ToolResult]:
            t0 = time.monotonic()
            res = await self.svc.registry.execute(c.name, c.input, ctx, self.svc, timeout_s=timeout)
            log.info("tool_done", tool=c.name, ok=not res.is_error, ms=int((time.monotonic() - t0) * 1000))
            return c, res

        return list(await asyncio.gather(*(one(c) for c in calls)))

    async def run_turn(
        self, ctx: UserContext, text: str, conversation_id: str | None, request_id: str | None = None
    ) -> AsyncIterator[Event]:
        cfg = self.svc.config
        profile = profile_for(ctx.role)
        prof = cfg.llm.profiles[profile]
        guest = not ctx.is_member_plus
        try:
            status = await self.svc.budget.check(
                guest=guest, session_public_id=ctx.session_id, account_id=ctx.account_id
            )
            if status.alert:
                log.warning("budget_month_alert", month_cost=round(status.month_cost, 3))
        except BudgetExceeded as e:
            metrics.CHAT_TURNS.labels(ctx.role.value, "budget").inc()
            yield {"type": "error", "message": e.user_message}
            return

        conv = await self.conversations.open(conversation_id, ctx, profile, prof.model)
        yield {"type": "conversation", "id": conv.id}
        limit = cfg.llm.budgets.max_input_chars_per_message
        body = text.strip()
        truncated = len(body) > limit
        body = body[:limit] + ("\n[...tin nhắn đã bị cắt bớt vì quá dài]" if truncated else "")
        user_msg = user_text(f"{user_header(ctx, clock.today())}\n{body}")
        await self.conversations.append(conv.id, user_msg)
        history = await self.conversations.messages(conv.id)

        system = system_prompt(ctx.role, cfg.lab.name)
        tools = self.svc.registry.specs_for(ctx.role)
        uctx = UsageContext(
            profile=profile,
            session_public_id=ctx.session_id,
            account_id=ctx.account_id,
            request_id=request_id,
        )
        model, degraded = conv.model, conv.degraded
        ui_actions: list[dict[str, Any]] = []
        log.info("chat_turn", conversation_id=conv.id, role=ctx.role.value, chars=len(body), model=model)

        for _iteration in range(cfg.llm.max_tool_iterations):
            final: LLMResponse | None = None
            try:
                async for ev in self.svc.llm.stream(
                    profile, system, tools, history, uctx, model=model, degraded=degraded
                ):
                    if isinstance(ev, TextDelta):
                        yield {"type": "text", "text": ev.text}
                    elif isinstance(ev, Reset):
                        yield {"type": "reset"}
                    elif isinstance(ev, Final):
                        final = ev.response
            except LLMError as e:
                metrics.CHAT_TURNS.labels(ctx.role.value, "llm_error").inc()
                log.error("chat_llm_error", kind=e.kind, request_id=e.request_id)
                yield {"type": "error", "message": LLMUnavailable.user_message}
                return
            assert final is not None
            if final.degraded and (final.model != model or not degraded):
                model, degraded = final.model, True
                await self.conversations.set_model(
                    conv.id, model, True
                )  # ở lại model fallback đến hết hội thoại

            assistant = final.assistant_message()
            if final.stop == "refusal":
                assistant = {"role": "assistant", "content": [{"type": "text", "text": REFUSAL_ANSWER}]}
                await self.conversations.append(conv.id, assistant)
                yield {"type": "reset"}
                yield {"type": "text", "text": REFUSAL_ANSWER}
                log.warning("chat_refusal", conversation_id=conv.id, model=final.model)
                break
            await self.conversations.append(conv.id, assistant)
            history.append(assistant)

            if not final.tool_calls:
                if final.stop == "max_tokens":
                    yield {"type": "text", "text": MAX_TOKENS_NOTE}
                break

            if final.stop == "max_tokens":
                # input tool có thể bị cắt cụt: không chạy, trả lỗi cho mọi tool_use để lịch sử hợp lệ
                results = [
                    tool_result(c.id, "Tham số bị cắt cụt, hãy gọi lại ngắn gọn hơn.", True)
                    for c in final.tool_calls
                ]
            else:
                for c in final.tool_calls:
                    yield {"type": "tool", "name": c.name, "status": "start"}
                done = await self._run_tools(final.tool_calls, ctx)
                results = []
                for c, res in done:
                    yield {"type": "tool", "name": c.name, "status": "error" if res.is_error else "done"}
                    ui_actions.extend(res.ui_actions)
                    results.append(tool_result(c.id, res.llm_text(), res.is_error))
            msg = tool_results_message(results)  # TẤT CẢ tool_result trong MỘT tin nhắn user
            await self.conversations.append(conv.id, msg)
            history.append(msg)
        else:
            note = {"role": "assistant", "content": [{"type": "text", "text": UNFINISHED_ANSWER}]}
            await self.conversations.append(conv.id, note)
            yield {"type": "text", "text": "\n\n" + UNFINISHED_ANSWER}
            log.warning("chat_max_iterations", conversation_id=conv.id)

        for a in ui_actions:
            yield {"type": "ui_action", "action": a}  # không trộn: action có trường "type" riêng
        metrics.CHAT_TURNS.labels(ctx.role.value, "ok").inc()
        yield {"type": "done", "conversation_id": conv.id, "degraded": degraded}


async def collect(events: AsyncIterator[Event]) -> dict[str, Any]:
    """Gom sự kiện thành câu trả lời cuối (dùng cho eval/CLI)."""
    text: list[str] = []
    tools: list[str] = []
    out: dict[str, Any] = {}
    async for ev in events:
        t = ev["type"]
        if t == "text":
            text.append(ev["text"])
        elif t == "reset":
            text.clear()
        elif t == "tool" and ev["status"] == "start":
            tools.append(ev["name"])
        elif t in ("done", "error", "conversation"):
            out[t] = ev
    out["text"] = "".join(text)
    out["tools"] = tools
    return out


__all__ = ["AgentRunner", "LabAgentError", "collect"]
