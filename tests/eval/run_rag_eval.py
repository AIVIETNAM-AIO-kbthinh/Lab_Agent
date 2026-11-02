"""Eval RAG cho KB: `labagent eval rag [--retrieval-only] [--only id1,id2] [--limit N] [--concurrency 3]`.

Hai tầng:
1. Retrieval (chỉ tốn embedding + rerank): với câu hỏi nguyên văn, đo Hit@1/3/6, MRR và context recall@6
   (top-6 chunk có chứa đủ ý chính hay không), so sánh có rerank và chỉ RRF. Kiểm tra rò rỉ: khách không được
   nhận chunk `member` hay chunk chứa nội dung nội bộ.
2. Trả lời end-to-end (tốn tiền LLM, ~0,002 USD/câu khách): chạy agent thật theo role, chấm ý chính trong câu
   trả lời, câu không có trong KB phải nói "không có thông tin", câu nội bộ hỏi bởi khách không được lộ.

Đầu ra: var/eval/rag-<thời điểm>.json + .md.
"""

from __future__ import annotations

import argparse
import asyncio
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

QUESTIONS = Path(__file__).with_name("rag_questions.yaml")
K = (1, 3, 6)
REFUSAL = [
    "khong tim thay",
    "chua tim thay",
    "khong co thong tin",
    "chua co thong tin",
    "khong co du lieu",
    "chua co du lieu",
    "khong de cap",
    "khong thay thong tin",
    "chua thay thong tin",
    "khong co trong",
    "chua duoc cong bo",
    "khong duoc cong bo",
    "chua duoc cap nhat",
    "khong nam trong",
    "khong hien thi",
    "chua hien thi",
    "khong cong bo",
    "chua cong bo",
    "chua thay",
    "khong thay",
    "chua ghi nhan",
    "khong ghi nhan",
    "khong nhac den",
    "chua nhac den",
    "khong duoc nhac",
    "not mentioned",
    "no information",
    "not find",
    "couldn't find",
    "could not find",
    "don't have",
    "do not have",
    "not available",
]
# khách hỏi nội dung nội bộ: câu trả lời đúng là từ chối/hướng đăng nhập (không bắt buộc, chỉ để thống kê)
MEMBER_HINT = [
    "dang nhap",
    "thanh vien",
    "noi bo",
    "khong co thong tin",
    "khong tim thay",
    "chua co thong tin",
]


def norm(text: str) -> str:
    """Không dấu, chữ thường, bỏ ký tự markdown để so khớp ý chính."""
    return ascii_key(re.sub(r"[*_`#>|]", "", text or ""))


def has_all(text: str, groups: list[list[str]]) -> list[list[str]]:
    """Trả về các nhóm ý chính KHÔNG tìm thấy."""
    t = norm(text)
    return [g for g in groups if not any(norm(alt) in t for alt in g)]


def source_rank(hits: list[dict[str, Any]], sources: list[str]) -> int | None:
    want = [norm(s) for s in sources]
    for i, h in enumerate(hits, 1):
        loc = norm(h["location"])
        if any(w in loc for w in want):
            return i
    return None


async def retrieve(svc: Any, q: dict[str, Any], rerank: bool) -> tuple[list[dict[str, Any]], bool]:
    svc.retriever.cfg.enabled = rerank
    res = await svc.retriever.search(q["q"], Role(q["role"]))
    for _ in range(3 if rerank else 0):  # API rerank đôi khi 503 → retriever tự rơi về RRF; thử lại
        if res.reranked or len(res.hits) <= 1:
            break
        await asyncio.sleep(2)
        res = await svc.retriever.search(q["q"], Role(q["role"]))
    return [
        {
            "location": str(h.payload.get("location", "")),
            "access": h.payload.get("access"),
            "heading": h.payload.get("heading"),
            "score": round(h.score, 4),
            "text": h.text,
        }
        for h in res.hits
    ], res.reranked


def grade_retrieval(q: dict[str, Any], got: tuple[list[dict[str, Any]], bool]) -> dict[str, Any]:
    hits, reranked = got
    out: dict[str, Any] = {"top": [f"{h['access']} {h['location']}" for h in hits], "reranked": reranked}
    if q.get("kind", "answer") == "no_leak":
        leaked = [h["location"] for h in hits if h["access"] != "public"]
        ctx = norm(" ".join(h["text"] for h in hits))
        leaked += [f"text:{s}" for s in q.get("leak", []) if norm(s) in ctx]
        out["leak"] = leaked
        return out
    if q.get("kind", "answer") != "answer":
        return out
    rank = source_rank(hits, q.get("sources", []))
    out["rank"] = rank
    out["missing_in_context"] = has_all(" ".join(h["text"] for h in hits), q.get("key_points", []))
    out["context_ok"] = not out["missing_in_context"]
    return out


def grade_answer(q: dict[str, Any], answer: str, tools: list[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    kind = q.get("kind", "answer")
    t = norm(answer)
    if not t:
        return False, ["không có câu trả lời"]
    if kind == "answer":
        if "search_kb" not in tools:
            reasons.append(f"không gọi search_kb (đã gọi {tools})")
        for g in has_all(answer, q.get("key_points", [])):
            reasons.append(f"thiếu ý {g}")
    elif kind == "refuse":
        if not any(p in t for p in REFUSAL):
            reasons.append("không nói rõ là không có thông tin (có thể bịa)")
    elif kind == "no_leak":
        for s in q.get("leak", []):
            if norm(s) in t:
                reasons.append(f"lộ nội dung nội bộ '{s}'")
    for pat in q.get("must_not") or []:
        if re.search(pat, answer):
            reasons.append(f"vi phạm must_not /{pat}/")
    return not reasons, reasons


def _tool_queries(messages: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    names, queries = [], []
    for m in messages:
        if m["role"] == "assistant":
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    names.append(b["name"])
                    if b["name"] == "search_kb":
                        queries.append(str((b.get("input") or {}).get("query", "")))
    return names, queries


async def answer_one(
    svc: Any, runner: AgentRunner, q: dict[str, Any], i: int, member: Any, sem: asyncio.Semaphore
) -> dict[str, Any]:
    async with sem:
        role = Role(q["role"])
        m = member if role != Role.GUEST else None
        sid = f"rag{int(time.time())}{i:03d}".ljust(32, "0")[:32]
        ctx = UserContext(role, sid, email=m.email if m else None, member=m, dev_login=bool(m))
        t0 = time.monotonic()
        out = await collect(runner.run_turn(ctx, q["q"], None, request_id=f"rag-{q['id']}"))
        wall = time.monotonic() - t0
        conv_id = (out.get("conversation") or {}).get("id")
        names, queries = _tool_queries(await runner.conversations.messages(conv_id)) if conv_id else ([], [])
        async with svc.sm() as s:
            cost = float(
                await s.scalar(
                    select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(
                        LLMUsage.session_public_id == sid
                    )
                )
                or 0.0
            )
        ok, reasons = grade_answer(q, out["text"], names)
        if out.get("error"):
            ok, reasons = False, [*reasons, f"lỗi: {out['error'].get('message')}"]
        return {
            "ok": ok,
            "reasons": reasons,
            "answer": out["text"],
            "tools": names,
            "kb_queries": queries,
            "wall_s": round(wall, 2),
            "cost_usd": round(cost, 5),
        }


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "—"


def retrieval_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    ans = [r for r in rows if r.get("kind", "answer") == "answer"]
    n = len(ans)
    ranks = [r[key].get("rank") for r in ans]
    out: dict[str, Any] = {"n": n}
    for k in K:
        out[f"hit@{k}"] = sum(1 for x in ranks if x is not None and x <= k)
    out["mrr"] = round(sum(1 / x for x in ranks if x) / n, 3) if n else 0.0
    out["context_recall@6"] = sum(1 for r in ans if r[key].get("context_ok"))
    leaks = [r for r in rows if r.get("kind") == "no_leak"]
    out["leak_n"] = len(leaks)
    out["leaked"] = sum(1 for r in leaks if r[key].get("leak"))
    out["reranked"] = sum(1 for r in rows if r[key].get("reranked"))
    return out


async def main(a: argparse.Namespace) -> int:
    from app.config import get_loaded
    from app.services import build_services

    loaded = get_loaded()
    clock.configure(loaded.config.timezone)
    svc = build_services(loaded, pool_size=8)
    questions: list[dict[str, Any]] = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    if a.only:
        wanted = set(a.only.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    if a.limit:
        questions = questions[: a.limit]
    rerank_cfg = svc.retriever.cfg.enabled
    rows: list[dict[str, Any]] = [{**q, "kind": q.get("kind", "answer")} for q in questions]
    try:
        # ---------------- tầng 1: retrieval
        for r in rows:
            r["ret_rerank"] = grade_retrieval(r, await retrieve(svc, r, rerank=True))
            r["ret_rrf"] = grade_retrieval(r, await retrieve(svc, r, rerank=False))
            rr = r["ret_rerank"]
            mark = (
                ("✓" if rr.get("rank") and rr["rank"] <= 6 else "✗")
                if r["kind"] == "answer"
                else ("✗ LEAK" if rr.get("leak") else "✓")
                if r["kind"] == "no_leak"
                else "·"
            )
            print(f"[retrieval] {mark} {r['id']:5} rank={rr.get('rank')} ctx={rr.get('context_ok')}")
        svc.retriever.cfg.enabled = rerank_cfg

        # ---------------- tầng 2: trả lời end-to-end
        if not a.retrieval_only:
            member = None
            try:
                snap = await svc.members.snapshot(force=True)
                member = next((m for m in snap.active if m.role == "member"), None)
            except Exception as e:  # không có Google Sheets vẫn chạy được: thành viên không tên
                print(f"(không đọc được Members: {type(e).__name__}; hỏi với vai trò member không tên)")
            runner = AgentRunner(svc)
            sem = asyncio.Semaphore(a.concurrency)

            async def run(i: int, r: dict[str, Any]) -> None:
                try:
                    r["answer"] = await answer_one(svc, runner, r, i, member, sem)
                except Exception as e:
                    r["answer"] = {
                        "ok": False,
                        "reasons": [f"exception {type(e).__name__}: {e}"],
                        "answer": "",
                    }
                ans = r["answer"]
                print(
                    f"[answer]    {'✓' if ans['ok'] else '✗'} {r['id']:5} {ans.get('wall_s', 0):5.1f}s "
                    f"{'; '.join(ans['reasons'])}"
                )

            await asyncio.gather(*(run(i, r) for i, r in enumerate(rows)))
    finally:
        await svc.aclose()

    # ---------------- tổng hợp
    s_rr, s_rrf = retrieval_summary(rows, "ret_rerank"), retrieval_summary(rows, "ret_rrf")
    answered = [r for r in rows if "answer" in r]
    cats: dict[str, list[dict[str, Any]]] = {}
    for r in answered:
        cats.setdefault(r["cat"], []).append(r)
    passed = sum(r["answer"]["ok"] for r in answered)
    summary = {
        "at": clock.now().isoformat(),
        "questions": len(rows),
        "retrieval_rerank": s_rr,
        "retrieval_rrf": s_rrf,
        "answer_passed": passed,
        "answer_total": len(answered),
        "answer_by_cat": {c: [sum(x["answer"]["ok"] for x in v), len(v)] for c, v in cats.items()},
        "avg_cost_usd": round(
            sum(r["answer"].get("cost_usd", 0) for r in answered) / max(1, len(answered)), 5
        ),
        "avg_wall_s": round(sum(r["answer"].get("wall_s", 0) for r in answered) / max(1, len(answered)), 2),
        "models": {k: v.model for k, v in loaded.config.llm.profiles.items()},
        "embedding": loaded.config.embedding.model,
        "rerank": loaded.config.rerank.model,
    }
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock.now().strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"rag-{stamp}"
    base.with_suffix(".json").write_text(
        json.dumps({"summary": summary, "results": rows}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    base.with_suffix(".md").write_text(report_md(stamp, summary, rows), encoding="utf-8")
    print("\n" + report_md(stamp, summary, rows).split("## Chi tiết")[0])
    print(f"→ {base}.md")
    return 0


def report_md(stamp: str, s: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    rr, rf = s["retrieval_rerank"], s["retrieval_rrf"]
    n = rr["n"]
    lines = [
        f"# Eval RAG — {stamp}",
        "",
        f"{s['questions']} câu · embedding `{s['embedding']}` · rerank `{s['rerank']}` · "
        f"guest `{s['models'].get('guest_agent')}` · member `{s['models'].get('member_agent')}`",
        "",
        "## Retrieval (câu hỏi nguyên văn → top 6 chunk đưa cho LLM)",
        "",
        "| Chỉ số | Hybrid + rerank | Chỉ hybrid (RRF) |",
        "|---|---|---|",
    ]
    for k in K:
        lines.append(f"| Hit@{k} (đúng nguồn) | {_pct(rr[f'hit@{k}'], n)} | {_pct(rf[f'hit@{k}'], n)} |")
    lines += [
        f"| MRR@6 | {rr['mrr']} | {rf['mrr']} |",
        f"| Context recall@6 (đủ ý chính trong context) | {_pct(rr['context_recall@6'], n)} | "
        f"{_pct(rf['context_recall@6'], n)} |",
        f"| Rò rỉ nội bộ cho khách | {rr['leaked']}/{rr['leak_n']} | {rf['leaked']}/{rf['leak_n']} |",
        "",
        f"Rerank thực sự chạy ở {rr['reranked']}/{s['questions']} câu (còn lại API rerank lỗi → đã rơi về RRF).",
        "",
    ]
    if s["answer_total"]:
        lines += [
            "## Trả lời end-to-end (agent thật)",
            "",
            f"- Đạt: **{_pct(s['answer_passed'], s['answer_total'])}**",
            f"- Chi phí TB/câu: {s['avg_cost_usd']:.4f} USD · thời gian TB: {s['avg_wall_s']} s",
            "",
            "| Nhóm | Đạt |",
            "|---|---|",
        ]
        for c, (ok, tot) in s["answer_by_cat"].items():
            lines.append(f"| {c} | {_pct(ok, tot)} |")
        lines.append("")
    lines += [
        "## Chi tiết",
        "",
        "| Câu | Role | Loại | Rank (rerank/RRF) | Context | Trả lời | Lý do | Truy vấn search_kb |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        a = r.get("answer") or {}
        rr_, rf_ = r["ret_rerank"], r["ret_rrf"]
        if r["kind"] == "answer":
            rank = f"{rr_.get('rank') or '–'}/{rf_.get('rank') or '–'}"
            ctx = "✅" if rr_.get("context_ok") else "❌"
        elif r["kind"] == "no_leak":
            rank, ctx = "", ("❌ lộ" if rr_.get("leak") else "✅ không lộ")
        else:
            rank, ctx = "", ""
        ok = "" if not a else ("✅" if a["ok"] else "❌")
        q = "; ".join(a.get("kb_queries") or [])
        lines.append(
            f"| {r['id']} | {r['role']} | {r['kind']} | {rank} | {ctx} | {ok} | "
            f"{'; '.join(a.get('reasons') or [])} | {q.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"
