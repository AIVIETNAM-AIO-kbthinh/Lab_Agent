"""web_crawl quick (02:00 hằng ngày) / full (thứ 2 03:00) + op outbox cho nút "Cập nhật ngay" trên admin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.jobs.alerts import alert_admins
from app.jobs.base import JobContext, job
from app.outbox.steps import OpKind, StepContext, register
from app.rag.crawler import CrawlReport, HttpFetcher, WebCrawler

if TYPE_CHECKING:
    from app.services import Services


def make_crawler(svc: Services, source_id: str | None = None) -> tuple[WebCrawler, HttpFetcher]:
    cfg = svc.config
    sources = [s for s in cfg.web_sources if source_id in (None, s.id)]
    if not sources:
        raise KeyError(f"Không có web_source '{source_id}'")
    src = sources[0]
    contact = src.bot_contact or (cfg.auth.admin_emails()[0] if cfg.auth.admins else "admin")
    fetcher = HttpFetcher(src, f"LabAgentBot/0.1 (+{cfg.lab.id}; contact: {contact})")
    crawler = WebCrawler(
        src,
        cfg.web_base_url(src),
        fetcher,
        svc.sm,
        svc.indexer,
        cfg.var_dir / "web_snapshots",
        cfg.retention.web_snapshots_keep,
    )
    return crawler, fetcher


async def _report(svc: Services, rep: CrawlReport, dry_run: bool) -> dict[str, Any]:
    out = {**rep.stats(), **rep.detail}
    if dry_run:
        return out
    await svc.audit.log(
        "system",
        "system",
        f"web_crawl_{rep.mode}",
        f"run {rep.run_id}",
        rep.status in ("APPLIED", "DRY_RUN"),
        out,
    )
    if rep.status == "BLOCKED":
        await alert_admins(
            svc,
            "Crawl website bị chặn bởi chốt an toàn",
            f"{rep.detail.get('reason')}\nMới: {rep.new}, đổi: {rep.changed}, sắp xoá: {rep.deleted}.\n"
            f"Nếu thay đổi là đúng (web vừa đổi giao diện), xác nhận bằng:\n  labagent crawl --apply-run {rep.run_id}\n"
            f"hoặc trên trang /admin/kb/web.",
            dedupe_key=f"crawl_blocked:{rep.run_id}",
        )
    return out


@job("web_crawl")
async def web_crawl(ctx: JobContext) -> dict[str, Any]:
    mode = ctx.params.get("mode", "quick")
    results = {}
    for src in ctx.svc.config.web_sources:
        crawler, fetcher = make_crawler(ctx.svc, src.id)
        try:
            rep = await crawler.run(mode, dry_run=ctx.dry_run, force=bool(ctx.params.get("force")))
        finally:
            await fetcher.aclose()
        results[src.id] = await _report(ctx.svc, rep, ctx.dry_run)
    return results


@job("web_crawl_quick")
async def web_crawl_quick(ctx: JobContext) -> dict[str, Any]:
    ctx.params.setdefault("mode", "quick")
    return await web_crawl(ctx)


@job("web_crawl_full")
async def web_crawl_full(ctx: JobContext) -> dict[str, Any]:
    ctx.params["mode"] = "full"
    return await web_crawl(ctx)


# ------------------------------------------------------------------ op outbox (worker chạy, API chỉ xếp hàng)


async def _op_crawl(sc: StepContext) -> dict[str, Any]:
    from app import clock

    ctx = JobContext(sc.svc, clock.today(), False, {"mode": sc.payload.get("mode", "quick")})
    return await web_crawl(ctx)


async def _op_apply(sc: StepContext) -> dict[str, Any]:
    run_id = int(sc.payload["run_id"])
    source_id = sc.payload.get("source_id")
    crawler, fetcher = make_crawler(sc.svc, source_id)
    try:
        rep = await crawler.apply_run(run_id)
    finally:
        await fetcher.aclose()
    await sc.svc.audit.log(sc.actor, "admin", "web_crawl_apply_run", f"run {run_id}", True, rep.stats())
    return rep.stats()


register(OpKind("web_crawl", [("crawl", _op_crawl)], max_attempts=2))
register(OpKind("web_crawl_apply", [("apply", _op_apply)], max_attempts=2))
