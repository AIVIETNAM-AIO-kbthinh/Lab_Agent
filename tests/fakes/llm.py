"""FakeLLM adapter (kịch bản), FakeEmbedder (bag-of-words băm), MemoryStore (vector store trong bộ nhớ)."""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.config import ModelCap
from app.llm.types import (
    CallOptions,
    Final,
    LLMError,
    LLMResponse,
    Msg,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
)
from app.rag.sparse import tokenize
from app.rag.store import ChunkPoint, Hit, SearchFilter

Script = Callable[[str, list[Msg], list[ToolSpec]], LLMResponse | LLMError]


def reply(
    text: str = "",
    tool_calls: list[tuple[str, dict[str, Any]]] | None = None,
    model: str = "fake",
    stop: str | None = None,
) -> LLMResponse:
    calls = [ToolCall(f"call_{i}_{name}", name, args) for i, (name, args) in enumerate(tool_calls or [])]
    return LLMResponse(
        model=model,
        provider="fake",
        text=text,
        tool_calls=calls,
        stop=stop or ("tool_use" if calls else "end_turn"),  # type: ignore[arg-type]
        usage=Usage(1000, 100),
        first_token_ms=50,
        latency_ms=100,
    )


class FakeAdapter:
    """Mỗi lời gọi lấy bước kế tiếp của kịch bản; ghi lại system/tools/messages để kiểm tra."""

    def __init__(self, provider: str, steps: list[Script | LLMResponse | LLMError] | None = None) -> None:
        self.provider = provider
        self.steps = list(steps or [])
        self.calls: list[dict[str, Any]] = []

    def _next(self, model: str, messages: list[Msg], tools: list[ToolSpec]) -> LLMResponse | LLMError:
        if not self.steps:
            return reply("(hết kịch bản)", model=model)
        step = self.steps.pop(0)
        res = step(model, messages, tools) if callable(step) else step
        if isinstance(res, LLMResponse):
            res = LLMResponse(**{**res.__dict__, "model": model, "provider": self.provider})
        return res

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
        self.calls.append(
            {
                "model": model,
                "system": system,
                "tools": [t.name for t in tools],
                "messages": [dict(m) for m in messages],
            }
        )
        res = self._next(model, messages, tools)
        if isinstance(res, LLMError):
            raise res
        if res.text:
            yield TextDelta(res.text)
        yield Final(res)

    async def create(
        self,
        model: str,
        cap: ModelCap,
        system: str,
        tools: list[ToolSpec],
        messages: list[Msg],
        opts: CallOptions,
    ) -> LLMResponse:
        final = None
        async for ev in self.stream(model, cap, system, tools, messages, opts, 10):
            if isinstance(ev, Final):
                final = ev.response
        assert final is not None
        return final


class FakeEmbedder:
    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0] * self.dimensions
            for w in tokenize(t):
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)  # noqa: S324
                v[h % self.dimensions] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class MemoryStore:
    def __init__(self) -> None:
        self.points: dict[str, ChunkPoint] = {}
        self.upserts = 0

    async def ensure(self, *_: Any, **__: Any) -> None:
        return None

    async def upsert(self, points: list[ChunkPoint]) -> None:
        self.upserts += len(points)
        for p in points:
            self.points[p.point_id] = p

    async def delete_doc(self, doc_id: str, keep_hash: str | None = None) -> None:
        for pid in [k for k, p in self.points.items() if p.doc_id == doc_id and p.content_hash != keep_hash]:
            del self.points[pid]

    def _match(self, p: ChunkPoint, f: SearchFilter) -> bool:
        pl = p.payload
        if pl.get("access") not in f.access_in:
            return False
        if f.doc_type and pl.get("doc_type") != f.doc_type:
            return False
        day = pl.get("doc_day")
        if f.day_from is not None and (day is None or day < f.day_from):
            return False
        return not (f.day_to is not None and (day is None or day > f.day_to))

    async def hybrid(
        self,
        dense: list[float],
        sparse: tuple[list[int], list[float]],
        flt: SearchFilter,
        prefetch: int,
        limit: int,
    ) -> list[Hit]:
        cands = [p for p in self.points.values() if self._match(p, flt)]
        qs = dict(zip(*sparse, strict=True))
        d_rank = sorted(cands, key=lambda p: -sum(a * b for a, b in zip(dense, p.dense, strict=True)))[
            :prefetch
        ]
        s_rank = sorted(cands, key=lambda p: -sum(qs.get(i, 0) * v for i, v in zip(*p.sparse, strict=True)))[
            :prefetch
        ]
        score: dict[str, float] = {}
        for ranking in (d_rank, s_rank):
            for r, p in enumerate(ranking):
                score[p.point_id] = score.get(p.point_id, 0.0) + 1.0 / (60 + r + 1)
        ranked = sorted(score.items(), key=lambda x: -x[1])[:limit]
        return [
            Hit(pid, s, {**self.points[pid].payload, "doc_id": self.points[pid].doc_id}) for pid, s in ranked
        ]

    async def count(self) -> int:
        return len(self.points)

    def docs(self) -> set[str]:
        return {p.doc_id for p in self.points.values()}
