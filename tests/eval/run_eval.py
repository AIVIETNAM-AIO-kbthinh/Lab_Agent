"""Chạy bộ eval P2 với model thật: `labagent eval run [--limit N] [--only id1,id2]`.

Tốn tiền LLM (≈ 30 lượt × ~0,01 USD). Yêu cầu: Drive/Sheets thật, KB đã index (kb_sync + crawl), DB, Qdrant.
Đầu ra: var/eval/<thời điểm>.json + .md; in tổng kết: số câu đạt, p95 thời gian ra token đầu, chi phí TB/lượt.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from app import clock
from app.agents.loop import AgentRunner, collect
from app.auth.roles import Role, UserContext
from app.db.models import LLMUsage
from app.domain.names import ascii_key

QUESTIONS = Path(__file__).with_name("questions.yaml")


def _tool_uses(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for m in messages:
        if m["role"] == "assistant":
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    out.append((b["name"], b.get("input") or {}))
    return out


def grade(q: dict[str, Any], answer: str, uses: list[tuple[str, dict[str, Any]]]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    called = [n for n, _ in uses]
    expect = q.get("expect_tools", [])
    allow = set(q.get("allow_tools", [])) | set(expect)
    if expect:
        if q.get("must_call_all"):
            if not set(expect) <= set(called):
                reasons.append(f"thiếu tool {sorted(set(expect) - set(called))}")
        elif not set(expect) & set(called):
            reasons.append(f"không gọi tool mong đợi {expect} (đã gọi {called})")
    bad = [n for n in called if n not in allow] if not expect or q.get("allow_tools") else []
    if bad:
        reasons.append(f"gọi tool không mong đợi {bad}")
    for tool, args in (q.get("expect_args") or {}).items():
        ok = any(
            n == tool and all(ascii_key(str(a.get(k, ""))) == ascii_key(str(v)) for k, v in args.items())
            for n, a in uses
        )
        if not ok:
            reasons.append(f"tham số {tool} sai (mong đợi {args})")
    text = ascii_key(answer)
    if not text.strip():
        reasons.append("không có câu trả lời")
    if q.get("check") != "tool_only" or q.get("key_points"):
        for group in q.get("key_points") or []:
            if not any(
                (re.search(alt, text) if alt.startswith("\\") else ascii_key(alt) in text) for alt in group
            ):
                reasons.append(f"thiếu ý chính {group}")
    for pat in q.get("must_not") or []:
        if re.search(pat, answer):
            reasons.append(f"vi phạm must_not /{pat}/")
    return not reasons, reasons


async def main(a: argparse.Namespace) -> int:
    from app.config import get_loaded
    from app.services import build_services

    loaded = get_loaded()
    clock.configure(loaded.config.timezone)
    svc = build_services(loaded)
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    if a.only:
        wanted = set(a.only.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    if a.limit:
        questions = questions[: a.limit]
    snap = await svc.members.snapshot(force=True)
    member = next((m for m in snap.active if m.role == "member"), None)
    leader = snap.leader()
    runner = AgentRunner(svc)
    results = []
    try:
        for i, q in enumerate(questions):
            role = Role(q["role"])
            m = {"member": member, "leader": leader}.get(q["role"])
            sid = f"eval{int(time.time())}{i:03d}".ljust(32, "0")[:32]
            ctx = UserContext(role, sid, email=m.email if m else None, member=m, dev_login=bool(m))
            t0 = time.monotonic()
            out = await collect(runner.run_turn(ctx, q["q"], None, request_id=f"eval-{q['id']}"))
            wall = time.monotonic() - t0
            conv_id = (out.get("done") or out.get("conversation") or {}).get("conversation_id") or (
                out.get("conversation") or {}
            ).get("id")
            uses = _tool_uses(await runner.conversations.messages(conv_id)) if conv_id else []
            async with svc.sm() as s:
                cost = float(
                    await s.scalar(
                        select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(
                            LLMUsage.session_public_id == sid
                        )
                    )
                    or 0.0
                )
                first = await s.scalar(
                    select(LLMUsage.first_token_ms)
                    .where(LLMUsage.session_public_id == sid)
                    .order_by(LLMUsage.id)
                    .limit(1)
                )
                models = sorted(
                    set(
                        (
                            await s.scalars(select(LLMUsage.model).where(LLMUsage.session_public_id == sid))
                        ).all()
                    )
                )
            ok, reasons = grade(q, out["text"], uses)
            results.append(
                {
                    "id": q["id"],
                    "role": q["role"],
                    "q": q["q"],
                    "ok": ok,
                    "reasons": reasons,
                    "tools": uses,
                    "answer": out["text"],
                    "error": out.get("error"),
                    "cost_usd": cost,
                    "first_token_ms": first,
                    "wall_s": round(wall, 2),
                    "models": models,
                }
            )
            print(f"{'✓' if ok else '✗'} {q['id']:6} {wall:5.1f}s {cost:.4f}$ {', '.join(reasons)}")
    finally:
        await svc.aclose()

    passed = sum(r["ok"] for r in results)
    ft = sorted(r["first_token_ms"] for r in results if r["first_token_ms"] is not None)
    p95 = ft[min(len(ft) - 1, round(0.95 * (len(ft) - 1)))] if ft else None
    avg_cost = sum(r["cost_usd"] for r in results) / max(1, len(results))
    summary = {
        "passed": passed,
        "total": len(results),
        "p95_first_token_ms": p95,
        "avg_cost_usd": round(avg_cost, 5),
        "profiles": {k: v.model for k, v in loaded.config.llm.profiles.items()},
        "at": clock.now().isoformat(),
    }
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    lines = [
        f"# Eval P2 — {stamp}",
        "",
        f"- Đạt: **{passed}/{len(results)}** (ngưỡng P2: ≥ 28/30)",
        f"- p95 token đầu: **{p95} ms** (ngưỡng < 8000 ms)",
        f"- Chi phí TB/lượt: **{avg_cost:.4f} USD**",
        "",
        "| Câu | Role | Đạt | Tool | Lý do | Thời gian | USD |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        tools = ", ".join(n for n, _ in r["tools"])
        lines.append(
            f"| {r['id']} | {r['role']} | {'✅' if r['ok'] else '❌'} | {tools} | "
            f"{'; '.join(r['reasons'])} | {r['wall_s']} s | {r['cost_usd']:.4f} |"
        )
    (out_dir / f"{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"\nĐạt {passed}/{len(results)} · p95 token đầu {p95} ms · TB {avg_cost:.4f} USD/lượt → {out_dir / stamp}.md"
    )
    return 0 if passed >= min(28, len(results)) else 1
