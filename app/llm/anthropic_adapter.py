"""Adapter Claude qua SDK `anthropic`, endpoint native /v1/messages (gateway chỉ đổi base_url).

- Tool `strict: true` khi model hỗ trợ; `tool_choice` luôn `auto` (Claude 5.x từ chối any/tool).
- System prompt + tool cố định theo role đặt `cache_control` (prefix cache: tools → system).
- Chỉ gửi `output_config.effort` khi model hỗ trợ (Haiku 4.5 không nhận).
- Block assistant được giữ NGUYÊN VẸN (raw) để gửi lại đúng như cũ.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.config import ModelCap
from app.llm.types import (
    Block,
    CallOptions,
    Final,
    LLMError,
    LLMResponse,
    Msg,
    Stop,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
    safe_tool_id,
)

_STOP: dict[str, Stop] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "stop_sequence": "end_turn",
    "pause_turn": "other",
}


def to_anthropic_messages(messages: list[Msg]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        raw = m.get("raw")
        if m["role"] == "assistant" and raw and raw.get("provider") == "anthropic":
            out.append({"role": "assistant", "content": raw["content"]})
            continue
        blocks: list[dict[str, Any]] = []
        for b in m["content"]:
            t = b["type"]
            if t == "text":
                if b["text"]:
                    blocks.append({"type": "text", "text": b["text"]})
            elif t == "tool_use":
                blocks.append(
                    {"type": "tool_use", "id": safe_tool_id(b["id"]), "name": b["name"], "input": b["input"]}
                )
            elif t == "tool_result":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": safe_tool_id(b["tool_use_id"]),
                        "content": b["content"],
                        "is_error": bool(b.get("is_error")),
                    }
                )
        if blocks:
            out.append({"role": m["role"], "content": blocks})
    return out


def tool_defs(tools: list[ToolSpec], strict: bool) -> list[dict[str, Any]]:
    defs: list[dict[str, Any]] = []
    for t in tools:
        d: dict[str, Any] = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        if strict:
            d["strict"] = True
        defs.append(d)
    return defs


def _classify(e: Exception) -> LLMError:
    rid = getattr(e, "request_id", None)
    if isinstance(e, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LLMError("auth", f"Anthropic auth: {e}", request_id=rid)
    if isinstance(e, anthropic.NotFoundError):
        return LLMError("not_found", f"Anthropic 404: {e}", request_id=rid)
    if isinstance(
        e, anthropic.BadRequestError | anthropic.UnprocessableEntityError | anthropic.RequestTooLargeError
    ):
        return LLMError("bad_request", f"Anthropic 400: {e}", request_id=rid)
    if isinstance(e, anthropic.APIStatusError | anthropic.APIConnectionError):
        return LLMError("unavailable", f"Anthropic: {type(e).__name__}: {e}", request_id=rid)
    return LLMError("unavailable", f"Anthropic: {type(e).__name__}: {e}")


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(self, api_key: str, base_url: str, timeout_s: float = 60, max_retries: int = 2) -> None:
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries
        )

    def build_params(
        self,
        model: str,
        cap: ModelCap,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        opts: CallOptions,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": opts.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": to_anthropic_messages(messages),
        }
        if tools:
            params["tools"] = tool_defs(tools, cap.strict_tools)
            params["tool_choice"] = {"type": "auto"}
        if cap.effort and opts.effort:
            params["output_config"] = {"effort": opts.effort}
        params.update(opts.extra)
        return params

    async def stream(
        self,
        model: str,
        cap: ModelCap,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        opts: CallOptions,
        first_token_timeout_s: float,
    ) -> AsyncIterator[StreamEvent]:
        params = self.build_params(model, cap, system, tools, messages, opts)
        t0 = time.monotonic()
        first_ms: int | None = None
        emitted = False
        try:
            async with self.client.messages.stream(**params) as stream:
                it = stream.__aiter__()
                while True:
                    try:
                        if first_ms is None:
                            event = await asyncio.wait_for(anext(it), timeout=first_token_timeout_s)
                        else:
                            event = await anext(it)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        raise LLMError(
                            "first_token_timeout", f"{model}: chưa có token sau {first_token_timeout_s}s"
                        ) from None
                    et = getattr(event, "type", "")
                    if first_ms is None and et in {"text", "content_block_start", "input_json", "thinking"}:
                        first_ms = int((time.monotonic() - t0) * 1000)
                    if event.type == "text" and event.text:
                        emitted = True
                        yield TextDelta(event.text)
                final = await stream.get_final_message()
        except LLMError as e:
            e.partial_text = emitted
            raise
        except anthropic.AnthropicError as e:
            err = _classify(e)
            err.partial_text = emitted
            raise err from e
        yield Final(self._to_response(final, model, first_ms, int((time.monotonic() - t0) * 1000)))

    async def create(
        self,
        model: str,
        cap: ModelCap,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        opts: CallOptions,
    ) -> LLMResponse:
        params = self.build_params(model, cap, system, tools, messages, opts)
        t0 = time.monotonic()
        try:
            msg = await self.client.messages.create(**params)
        except anthropic.AnthropicError as e:
            raise _classify(e) from e
        ms = int((time.monotonic() - t0) * 1000)
        return self._to_response(msg, model, ms, ms)

    @staticmethod
    def _to_response(msg: Any, model: str, first_ms: int | None, latency_ms: int) -> LLMResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        raw: list[Block] = []
        for b in msg.content:
            raw.append(b.model_dump(mode="json", exclude_none=True))
            if b.type == "text":
                text_parts.append(b.text)
            elif b.type == "tool_use":
                inp = b.input if isinstance(b.input, dict) else json.loads(json.dumps(b.input))
                calls.append(ToolCall(b.id, b.name, inp if isinstance(inp, dict) else {"__invalid__": inp}))
        u = msg.usage
        usage = Usage(
            input_tokens=int(u.input_tokens or 0),
            output_tokens=int(u.output_tokens or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        return LLMResponse(
            model=model,
            provider="anthropic",
            text="".join(text_parts),
            tool_calls=calls,
            stop=_STOP.get(str(msg.stop_reason), "other"),
            usage=usage,
            raw_content=raw,
            request_id=getattr(msg, "_request_id", None),
            first_token_ms=first_ms,
            latency_ms=latency_ms,
        )
