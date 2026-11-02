"""Điểm DUY NHẤT gọi LLM (mục 7.8).

profile → model → adapter theo model_capabilities. Chuyển sang fallback KHÁC HÃNG một lần (cờ degraded) khi
hết retry, stop_reason=refusal, hoặc quá first_token_timeout_s mà chưa có token. 400 không fallback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import BaseModel

from app import metrics
from app.config import LLMCfg, ModelCap, ProfileCfg
from app.llm.types import (
    CallOptions,
    Final,
    LLMError,
    LLMResponse,
    Msg,
    Reset,
    StreamEvent,
    TextDelta,
    ToolSpec,
    UsageContext,
    tool_result,
    tool_results_message,
    user_text,
)
from app.llm.usage import UsageStore, cost_usd, observe
from app.logging import get_logger

log = get_logger(__name__)


class Adapter(Protocol):
    provider: str

    def stream(
        self,
        model: str,
        cap: ModelCap,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        opts: CallOptions,
        first_token_timeout_s: float,
    ) -> AsyncIterator[StreamEvent]: ...

    async def create(
        self,
        model: str,
        cap: ModelCap,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        opts: CallOptions,
    ) -> LLMResponse: ...


class LLMClient:
    def __init__(self, cfg: LLMCfg, adapters: dict[str, Adapter], usage: UsageStore | None = None) -> None:
        self.cfg = cfg
        self.adapters = adapters
        self.usage = usage

    def profile(self, name: str) -> ProfileCfg:
        return self.cfg.profiles[name]

    def _adapter(self, model: str) -> tuple[Adapter, ModelCap]:
        cap = self.cfg.capability(model)
        return self.adapters[cap.adapter], cap

    async def _record(self, ctx: UsageContext, resp: LLMResponse) -> None:
        cap = self.cfg.capability(resp.model)
        cost = cost_usd(cap, resp.usage)
        observe(ctx, resp, cost)
        if self.usage is not None:
            try:
                await self.usage.record(ctx, resp, cost)
            except Exception as e:
                log.error("llm_usage_record_failed", error=str(e)[:200])

    async def stream(
        self,
        profile: str,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        ctx: UsageContext,
        *,
        model: str | None = None,
        degraded: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """Stream một lời gọi. `model` cho phép hội thoại đã fallback ở lại model fallback."""
        prof = self.profile(profile)
        current = model or prof.model
        is_degraded = degraded
        while True:
            adapter, cap = self._adapter(current)
            opts = CallOptions(max_tokens=prof.max_tokens, effort=prof.effort)
            emitted = False
            try:
                final: LLMResponse | None = None
                async for ev in adapter.stream(
                    current, cap, system, tools, messages, opts, self.cfg.first_token_timeout_s
                ):
                    if isinstance(ev, TextDelta):
                        emitted = True
                        yield ev
                    elif isinstance(ev, Final):
                        final = ev.response
                assert final is not None
                final.degraded = is_degraded
                await self._record(ctx, final)
                if final.stop == "refusal" and not is_degraded and self.cfg.fallback_for(current):
                    raise LLMError("refusal", f"{current}: refusal", partial_text=emitted)
                yield Final(final)
                return
            except LLMError as e:
                metrics.LLM_ERRORS.labels(current, e.kind).inc()
                fb = self.cfg.fallback_for(current)
                log.warning(
                    "llm_call_failed",
                    model=current,
                    kind=e.kind,
                    request_id=e.request_id,
                    error=str(e)[:300],
                    will_fallback=bool(fb and e.fallback_ok and not is_degraded),
                )
                if not (fb and e.fallback_ok and not is_degraded):
                    raise
                metrics.LLM_FALLBACKS.labels(current, fb).inc()
                if e.partial_text or emitted:
                    yield Reset(f"fallback:{e.kind}")
                current, is_degraded = fb, True

    async def complete(
        self,
        profile: str,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        ctx: UsageContext,
        *,
        model: str | None = None,
    ) -> LLMResponse:
        final: LLMResponse | None = None
        async for ev in self.stream(profile, system, tools, messages, ctx, model=model):
            if isinstance(ev, Final):
                final = ev.response
        assert final is not None
        return final

    async def structured(
        self,
        profile: str,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        ctx: UsageContext,
    ) -> BaseModel:
        """Trích dữ liệu có cấu trúc qua một tool strict có một tham số object, rồi validate lại bằng Pydantic.

        Cách này chạy được trên cả hai adapter (gateway có thể không chuyển tiếp structured outputs).
        """
        from app.tools.schema import strict_schema

        tool = ToolSpec("submit_result", "Gửi kết quả trích xuất theo đúng schema.", strict_schema(schema))
        sys = system + "\n\nLuôn trả kết quả bằng cách gọi tool submit_result đúng một lần."
        messages: list[Msg] = [user_text(prompt)]
        last_error = ""
        for _ in range(2):
            resp = await self.complete(profile, sys, [tool], messages, ctx)
            call = next((c for c in resp.tool_calls if c.name == "submit_result"), None)
            if call is None:
                last_error = "model không gọi tool submit_result"
            else:
                try:
                    return schema.model_validate(call.input)
                except Exception as e:  # pydantic.ValidationError
                    last_error = str(e)[:500]
            retry_msg = f"Kết quả chưa hợp lệ ({last_error}). Gọi lại submit_result đúng schema."
            follow = (
                tool_results_message([tool_result(c.id, retry_msg, is_error=True) for c in resp.tool_calls])
                if resp.tool_calls
                else user_text(retry_msg)
            )
            messages = [*messages, resp.assistant_message(), follow]
        raise LLMError("bad_request", f"structured output không hợp lệ: {last_error}")

    def describe(self) -> dict[str, Any]:
        return {
            name: {
                "model": p.model,
                "adapter": self.cfg.capability(p.model).adapter,
                "fallback": self.cfg.fallback_for(p.model),
            }
            for name, p in self.cfg.profiles.items()
        }
