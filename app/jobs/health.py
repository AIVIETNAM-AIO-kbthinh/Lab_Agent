"""health (hằng ngày): token Google, gateway LLM, op FAILED, chi phí LLM tháng, web lab lỗi nhiều ngày."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.db.models import OutboxOp, WebCrawlRun
from app.jobs.alerts import alert_admins
from app.jobs.base import JobContext, job
from app.llm.usage import month_window


async def check_google(ctx: JobContext) -> str | None:
    try:
        from app.google.oauth import load_credentials, refresh_credentials

        creds = load_credentials(ctx.svc.settings.google_token_file, refresh=False)
        refresh_credentials(creds, ctx.svc.settings.google_token_file)
        await ctx.svc.google.drive.about_email()
    except Exception as e:
        return f"Google: {type(e).__name__}: {str(e)[:300]}"
    return None


async def check_llm(ctx: JobContext) -> str | None:
    import openai

    s = ctx.svc.settings
    try:
        client = openai.AsyncOpenAI(
            api_key=s.llm_api_key.get_secret_value(),
            base_url=s.llm_openai_base_url,
            timeout=20,
            max_retries=1,
        )
        models = await client.models.list()
        ids = {m.id for m in models.data}
    except Exception as e:
        return f"Gateway LLM: {type(e).__name__}: {str(e)[:200]}"
    wanted = {p.model for p in ctx.svc.config.llm.profiles.values()} | {ctx.svc.config.embedding.model}
    missing = sorted(w for w in wanted if ids and w not in ids)
    return f"Gateway thiếu model: {', '.join(missing)}" if missing else None


@job("health")
async def health(ctx: JobContext) -> dict[str, Any]:
    svc = ctx.svc
    problems: list[str] = []
    for check in (check_google, check_llm):
        err = await check(ctx)
        if err:
            problems.append(err)
    async with svc.sm() as s:
        failed = int(
            await s.scalar(select(func.count()).select_from(OutboxOp).where(OutboxOp.status == "FAILED")) or 0
        )
        runs = (await s.scalars(select(WebCrawlRun).order_by(WebCrawlRun.started_at.desc()).limit(3))).all()
    if failed:
        problems.append(f"Có {failed} thao tác ghi FAILED trong outbox (xem /admin).")
    if len(runs) == 3 and all(r.status in ("ABORTED", "FAILED") for r in runs):
        problems.append("Website lab không crawl được 3 lần liên tiếp (web sập hoặc đổi cấu trúc).")
    start, _ = month_window()
    cost = await svc.usage.cost_since(start)
    budgets = svc.config.llm.budgets
    if cost >= budgets.lab_usd_per_month_alert:
        problems.append(
            f"Chi phí LLM tháng này {cost:.2f} USD ≥ ngưỡng cảnh báo {budgets.lab_usd_per_month_alert} USD "
            f"(dừng hẳn ở {budgets.lab_usd_per_month_hard_stop} USD)."
        )
    if problems and not ctx.dry_run:
        await alert_admins(
            svc,
            "Kiểm tra sức khoẻ hệ thống có vấn đề",
            "\n".join(f"- {p}" for p in problems),
            dedupe_key=f"health:{ctx.today.isoformat()}",
        )
    return {"ok": not problems, "problems": problems, "month_cost_usd": round(cost, 4), "failed_ops": failed}
