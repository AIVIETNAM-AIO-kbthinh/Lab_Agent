"""Adapter cho model qua endpoint OpenAI-compatible của gateway (Gemini chat).

Không có chế độ strict ⇒ tham số tool LUÔN được validate lại bằng Pydantic ở registry.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import openai

from app.config import ModelCap
from app.llm.types import (
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
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def to_openai_messages(system: str, messages: list[Msg]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "assistant":
            text = "".join(b["text"] for b in m["content"] if b["type"] == "text")
            calls = [
                {
                    "id": safe_tool_id(b["id"]),
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)},
                }
                for b in m["content"]
                if b["type"] == "tool_use"
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        texts = [b["text"] for b in m["content"] if b["type"] == "text" and b["text"]]
        for b in m["content"]:
            if b["type"] == "tool_result":
                content = b["content"]
                if b.get("is_error"):
                    content = json.dumps({"error": content}, ensure_ascii=False)
                out.append(
                    {"role": "tool", "tool_call_id": safe_tool_id(b["tool_use_id"]), "content": content}
                )
        if texts:
            out.append({"role": "user", "content": "\n\n".join(texts)})
    return out


def loose_schema(node: Any) -> Any:
    """Schema strict → dạng "lỏng" cho kênh OpenAI-compatible.

    Kênh Gemini của gateway âm thầm trả phản hồi rỗng khi schema có `anyOf [..., {"type": "null"}]`
    (đo 2026-10-16). Trường nullable được đổi về kiểu không-null và bỏ khỏi `required`; bỏ
    `additionalProperties`. Registry vẫn validate lại tham số bằng Pydantic.
    """
    if isinstance(node, list):
        return [loose_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k == "additionalProperties":
            continue
        out[k] = loose_schema(v)
    if "anyOf" in out:
        non_null = [x for x in out["anyOf"] if not (isinstance(x, dict) and x.get("type") == "null")]
        if len(non_null) == 1 and len(non_null) < len(out["anyOf"]):
            rest = {k: v for k, v in out.items() if k != "anyOf"}
            out = {**non_null[0], **rest, "_nullable": True}
    if isinstance(out.get("properties"), dict):
        props = out["properties"]
        out["required"] = [k for k in out.get("required", []) if not props.get(k, {}).get("_nullable")]
        out["properties"] = {
            k: {kk: vv for kk, vv in v.items() if kk != "_nullable"} for k, v in props.items()
        }
    return out


def tool_defs(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": loose_schema(t.input_schema),
            },
        }
        for t in tools
    ]


def _classify(e: Exception) -> LLMError:
    if isinstance(e, openai.AuthenticationError | openai.PermissionDeniedError):
        return LLMError("auth", f"OpenAI-compat auth: {e}")
    if isinstance(e, openai.NotFoundError):
        return LLMError("not_found", f"OpenAI-compat 404: {e}")
    if isinstance(e, openai.BadRequestError | openai.UnprocessableEntityError):
        return LLMError("bad_request", f"OpenAI-compat 400: {e}")
    return LLMError("unavailable", f"OpenAI-compat: {type(e).__name__}: {e}")


def _parse_args(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return {"__invalid_json__": raw[:500]}
    return v if isinstance(v, dict) else {"__invalid_json__": raw[:500]}


class OpenAICompatAdapter:
    provider = "openai_compat"

    def __init__(self, api_key: str, base_url: str, timeout_s: float = 60, max_retries: int = 2) -> None:
        self.client = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries
        )

    def build_params(
        self, model: str, system: str, tools: list[ToolSpec], messages: list[Msg], opts: CallOptions
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(system, messages),
            "max_tokens": opts.max_tokens,
        }
        if tools:
            params["tools"] = tool_defs(tools)
            params["tool_choice"] = "auto"
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
        params = self.build_params(model, system, tools, messages, opts)
        t0 = time.monotonic()
        first_ms: int | None = None
        emitted = False
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage = Usage()
        rid: str | None = None
        try:
            stream = await asyncio.wait_for(
                self.client.chat.completions.create(
                    **params, stream=True, stream_options={"include_usage": True}
                ),
                timeout=first_token_timeout_s,
            )
            it = stream.__aiter__()
            while True:
                try:
                    chunk = await (
                        asyncio.wait_for(anext(it), first_token_timeout_s) if first_ms is None else anext(it)
                    )
                except StopAsyncIteration:
                    break
                rid = rid or getattr(chunk, "id", None)
                if chunk.usage:
                    u = chunk.usage
                    cached = (
                        int(getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0)
                        if u.prompt_tokens_details
                        else 0
                    )
                    usage = Usage(
                        input_tokens=int(u.prompt_tokens or 0) - cached,
                        output_tokens=int(u.completion_tokens or 0),
                        cache_read_tokens=cached,
                    )
                if not chunk.choices:
                    continue
                ch = chunk.choices[0]
                delta = ch.delta
                if first_ms is None and (delta.content or delta.tool_calls):
                    first_ms = int((time.monotonic() - t0) * 1000)
                if delta.content:
                    emitted = True
                    text.append(delta.content)
                    yield TextDelta(delta.content)
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
                if ch.finish_reason:
                    finish = ch.finish_reason
        except TimeoutError:
            raise LLMError(
                "first_token_timeout",
                f"{model}: chưa có token sau {first_token_timeout_s}s",
                partial_text=emitted,
            ) from None
        except openai.OpenAIError as e:
            err = _classify(e)
            err.partial_text = emitted
            raise err from e
        tool_calls = [
            ToolCall(c["id"] or f"call_{i}", c["name"], _parse_args(c["args"]))
            for i, c in sorted(calls.items())
            if c["name"]
        ]
        if not tool_calls and not text and usage.total == 0:
            # gateway "nuốt" request (vd schema không hỗ trợ): coi là lỗi để chuyển fallback khác hãng
            raise LLMError("unavailable", f"{model}: phản hồi rỗng từ gateway (finish={finish})")
        stop: Stop = "tool_use" if tool_calls else _STOP.get(finish or "stop", "other")
        yield Final(
            LLMResponse(
                model=model,
                provider=self.provider,
                text="".join(text),
                tool_calls=tool_calls,
                stop=stop,
                usage=usage,
                raw_content=None,
                request_id=rid,
                first_token_ms=first_ms,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        )

    async def create(
        self,
        model: str,
        cap: ModelCap,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        opts: CallOptions,
    ) -> LLMResponse:
        final: LLMResponse | None = None
        async for ev in self.stream(model, cap, system, tools, messages, opts, first_token_timeout_s=120):
            if isinstance(ev, Final):
                final = ev.response
        assert final is not None
        return final
