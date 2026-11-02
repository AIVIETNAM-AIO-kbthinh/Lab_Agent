"""`labagent smoke llm`: kiểm tra tính năng gateway hỗ trợ, ghi kết quả vào docs/gateway-compat.md.

Kiểm tra: gọi thường, streaming (thời gian token đầu), tool strict, prompt cache (cache_read_input_tokens),
structured output (messages.parse), effort, embedding, rerank. Tốn vài cent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel

from app import clock
from app.config import LoadedConfig
from app.llm.anthropic_adapter import AnthropicAdapter
from app.llm.embeddings import OpenAIEmbedder
from app.llm.openai_adapter import OpenAICompatAdapter
from app.llm.rerank import HttpReranker
from app.llm.types import CallOptions, Final, TextDelta, ToolSpec, user_text


@dataclass
class Check:
    target: str
    feature: str
    ok: bool | None  # None = không áp dụng
    detail: str


class Contact(BaseModel):
    ho_ten: str
    email: str
    so_luong: int


TOOL = ToolSpec(
    "query_equipment",
    "Tra thiết bị của lab theo tên.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "tên thiết bị"}},
        "required": ["name"],
        "additionalProperties": False,
    },
)
LONG_SYSTEM = "Bạn là trợ lý của phòng thí nghiệm. " + " ".join(
    f"Quy định {i}: thành viên phải giữ gìn thiết bị số {i}, trả đúng hạn, ghi sổ mượn đầy đủ, "
    f"không mang thiết bị ra khỏi lab khi chưa được leader đồng ý, báo ngay khi hỏng."
    for i in range(1, 160)
)


async def _anthropic_checks(ad: AnthropicAdapter, model: str, cap: Any) -> list[Check]:
    out: list[Check] = []
    opts = CallOptions(max_tokens=300)
    try:
        r = await ad.create(model, cap, "Trả lời ngắn.", [], [user_text("1+1 bằng mấy?")], opts)
        out.append(Check(model, "messages.create", True, f"{r.latency_ms} ms, stop={r.stop}"))
    except Exception as e:
        out.append(Check(model, "messages.create", False, f"{type(e).__name__}: {str(e)[:120]}"))
        return out
    try:
        t0 = time.monotonic()
        first: float | None = None
        total = 0.0
        async for ev in ad.stream(
            model, cap, "Trả lời ngắn.", [], [user_text("Kể tên 3 loại cảm biến.")], opts, 60
        ):
            if isinstance(ev, TextDelta) and first is None:
                first = time.monotonic() - t0
            if isinstance(ev, Final):
                total = time.monotonic() - t0
        out.append(Check(model, "streaming", True, f"token đầu {first or 0:.2f} s, tổng {total:.2f} s"))
    except Exception as e:
        out.append(Check(model, "streaming", False, f"{type(e).__name__}: {str(e)[:120]}"))
    try:
        r = await ad.create(
            model, cap, "Dùng tool khi cần tra thiết bị.", [TOOL], [user_text("Máy chiếu còn mấy cái?")], opts
        )
        call = r.tool_calls[0] if r.tool_calls else None
        ok = call is not None and isinstance(call.input.get("name"), str)
        out.append(
            Check(
                model,
                "tool use strict" if cap.strict_tools else "tool use",
                ok,
                f"input={call.input if call else None}",
            )
        )
    except Exception as e:
        out.append(Check(model, "tool use strict", False, f"{type(e).__name__}: {str(e)[:120]}"))
    try:
        reads, writes = [], []
        for _ in range(2):
            r = await ad.create(
                model,
                cap,
                LONG_SYSTEM,
                [],
                [user_text("Tóm tắt quy định 1 trong một câu.")],
                CallOptions(max_tokens=100),
            )
            reads.append(r.usage.cache_read_tokens)
            writes.append(r.usage.cache_write_tokens)
        out.append(
            Check(
                model,
                "prompt caching",
                reads[-1] > 0,
                f"lần 1: write={writes[0]} read={reads[0]}; lần 2: write={writes[1]} read={reads[1]}",
            )
        )
    except Exception as e:
        out.append(Check(model, "prompt caching", False, f"{type(e).__name__}: {str(e)[:120]}"))
    try:
        msg = await ad.client.messages.parse(
            model=model,
            max_tokens=400,
            output_format=Contact,
            messages=[
                {"role": "user", "content": "Trích: Nguyễn Văn A (a@gmail.com) muốn mượn 2 máy chiếu."}
            ],
        )
        parsed = msg.parsed_output
        out.append(Check(model, "structured output (messages.parse)", parsed is not None, f"{parsed}"))
    except Exception as e:
        out.append(
            Check(
                model,
                "structured output (messages.parse)",
                False,
                f"{type(e).__name__}: {str(e)[:120]} → dùng tool strict thay thế",
            )
        )
    if cap.effort:
        try:
            await ad.create(
                model,
                cap,
                "Trả lời ngắn.",
                [],
                [user_text("Chào")],
                CallOptions(max_tokens=200, effort="low"),
            )
            out.append(Check(model, "output_config.effort", True, "chấp nhận effort=low"))
        except Exception as e:
            out.append(Check(model, "output_config.effort", False, f"{type(e).__name__}: {str(e)[:120]}"))
    else:
        out.append(Check(model, "output_config.effort", None, "model không hỗ trợ (không gửi)"))
    return out


async def _openai_checks(ad: OpenAICompatAdapter, model: str, cap: Any) -> list[Check]:
    out: list[Check] = []
    try:
        r = await ad.create(
            model, cap, "Trả lời ngắn.", [], [user_text("1+1 bằng mấy?")], CallOptions(max_tokens=200)
        )
        out.append(
            Check(
                model,
                "chat.completions (stream)",
                True,
                f"{r.latency_ms} ms, token đầu {r.first_token_ms} ms",
            )
        )
        r = await ad.create(
            model,
            cap,
            "Dùng tool khi cần tra thiết bị.",
            [TOOL],
            [user_text("Máy chiếu còn mấy cái?")],
            CallOptions(max_tokens=300),
        )
        call = r.tool_calls[0] if r.tool_calls else None
        out.append(
            Check(model, "tool use (không strict)", call is not None, f"input={call.input if call else None}")
        )
    except Exception as e:
        out.append(Check(model, "chat.completions", False, f"{type(e).__name__}: {str(e)[:120]}"))
    return out


async def run_smoke(
    loaded: LoadedConfig, models: list[str] | None = None, out_path: str = "docs/gateway-compat.md"
) -> list[Check]:
    s, cfg = loaded.settings, loaded.config
    key = s.llm_api_key.get_secret_value()
    a_ad = AnthropicAdapter(key, s.llm_anthropic_base_url, 90)
    o_ad = OpenAICompatAdapter(key, s.llm_openai_base_url, 90)
    wanted = models or sorted(
        {p.model for p in cfg.llm.profiles.values()}
        | {m for m in (cfg.llm.fallback.anthropic, cfg.llm.fallback.openai_compat) if m}
    )
    checks: list[Check] = []
    for m in wanted:
        cap = cfg.llm.capability(m)
        checks += await (
            _anthropic_checks(a_ad, m, cap) if cap.adapter == "anthropic" else _openai_checks(o_ad, m, cap)
        )
    try:
        emb = OpenAIEmbedder(key, s.llm_openai_base_url, cfg.embedding)
        v = await emb.embed(["máy chiếu", "lịch trực"])
        checks.append(
            Check(
                cfg.embedding.model,
                "embedding",
                len(v[0]) == cfg.embedding.dimensions,
                f"{len(v)} vector × {len(v[0])} chiều",
            )
        )
    except Exception as e:
        checks.append(Check(cfg.embedding.model, "embedding", False, f"{type(e).__name__}: {str(e)[:120]}"))
    rr = HttpReranker(key, s.llm_openai_base_url, cfg.rerank)
    res = await rr.rerank("mượn máy chiếu", ["Quy định mượn máy chiếu", "Lịch trực tuần", "Máy in 3D"], 3)
    checks.append(Check(cfg.rerank.model, "rerank", bool(res), f"{res}"))
    await rr.aclose()
    write_report(checks, out_path, s.llm_anthropic_base_url, s.llm_openai_base_url)
    return checks


def write_report(checks: list[Check], out_path: str, a_url: str, o_url: str) -> None:
    mark = {True: "✅", False: "❌", None: "—"}
    lines = [
        "# Tương thích gateway LLM",
        "",
        f"> Sinh tự động bởi `labagent smoke llm` lúc {clock.now():%Y-%m-%d %H:%M} ({clock.tz().key}).",
        f"> Anthropic base URL: `{a_url}` · OpenAI-compatible base URL: `{o_url}`.",
        f"> SDK: anthropic {anthropic.__version__}.",
        "",
        "| Model | Tính năng | Kết quả | Chi tiết |",
        "|---|---|---|---|",
    ]
    for c in checks:
        detail = c.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{c.target}` | {c.feature} | {mark[c.ok]} | {detail} |")
    lines += [
        "",
        "## Hệ quả cho code",
        "",
        "- Prompt cache không được chuyển tiếp ⇒ code vẫn chạy, chỉ đắt hơn; theo dõi `cache_read_tokens` trong `llm_usage`.",
        "- Structured output (`messages.parse`) lỗi ⇒ `LLMClient.structured()` đã dùng tool strict một tham số object.",
        "- Tool strict không có trên kênh OpenAI-compatible ⇒ registry luôn validate lại tham số bằng Pydantic.",
        '- Kênh Gemini (OpenAI-compatible) **âm thầm trả phản hồi rỗng** khi schema tool có `anyOf [..., {"type": "null"}]` (phát hiện 2026-10-16) ⇒ `openai_adapter.loose_schema()` đổi về schema lỏng; phản hồi rỗng hoàn toàn được coi là lỗi để chuyển fallback.',
    ]
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
