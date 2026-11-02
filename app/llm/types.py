"""Kiểu dữ liệu trung lập giữa các adapter LLM.

Tin nhắn lưu dạng dict JSON (bảng messages.content_json):
    {"role": "user"|"assistant",
     "content": [ {"type": "text", "text": ...}
                | {"type": "tool_use", "id": ..., "name": ..., "input": {...}}
                | {"type": "tool_result", "tool_use_id": ..., "content": "...", "is_error": bool} ],
     "raw": {"provider": "anthropic", "model": ..., "content": [...]}   # chỉ assistant, giữ NGUYÊN VẸN block
    }
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Msg = dict[str, Any]
Block = dict[str, Any]
Stop = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class LLMResponse:
    model: str
    provider: str
    text: str
    tool_calls: list[ToolCall]
    stop: Stop
    usage: Usage
    raw_content: list[Block] | None = None
    request_id: str | None = None
    first_token_ms: int | None = None
    latency_ms: int = 0
    degraded: bool = False

    def assistant_message(self) -> Msg:
        content: list[Block] = []
        if self.text:
            content.append({"type": "text", "text": self.text})
        for tc in self.tool_calls:
            content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
        msg: Msg = {"role": "assistant", "content": content}
        if self.raw_content is not None:
            msg["raw"] = {"provider": self.provider, "model": self.model, "content": self.raw_content}
        return msg


# ------------------------------------------------------------------ sự kiện stream


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Reset:
    """Bỏ phần text đã stream (chuyển sang model fallback giữa chừng)."""

    reason: str


@dataclass(frozen=True)
class Final:
    response: LLMResponse


StreamEvent = TextDelta | Reset | Final


@dataclass(frozen=True)
class UsageContext:
    profile: str
    session_public_id: str | None = None
    account_id: str | None = None
    request_id: str | None = None


@dataclass
class CallOptions:
    max_tokens: int
    effort: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ helper dựng tin nhắn


def user_text(text: str) -> Msg:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def tool_results_message(results: list[Block]) -> Msg:
    """TẤT CẢ tool_result của một lượt nằm trong MỘT tin nhắn user."""
    return {"role": "user", "content": results}


def tool_result(tool_use_id: str, content: str, is_error: bool = False) -> Block:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}


_ID_OK = re.compile(r"[^a-zA-Z0-9_-]")


def safe_tool_id(tool_id: str) -> str:
    return _ID_OK.sub("_", tool_id)[:64] or "call"


class LLMError(Exception):
    """kind: unavailable | first_token_timeout | refusal | bad_request | auth | not_found."""

    def __init__(self, kind: str, message: str, *, request_id: str | None = None, partial_text: bool = False):
        super().__init__(message)
        self.kind = kind
        self.request_id = request_id
        self.partial_text = partial_text

    @property
    def fallback_ok(self) -> bool:
        return self.kind in {"unavailable", "first_token_timeout", "refusal", "not_found"}
