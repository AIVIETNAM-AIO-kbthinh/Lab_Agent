"""Registry job + runner chung: tiêm `today`, `dry_run`; log, metrics, alert khi lỗi."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from app import clock, metrics
from app.errors import DuplicateNames
from app.jobs.idempotency import Idempotency
from app.logging import bind, get_logger

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


@dataclass
class JobContext:
    svc: Services
    today: date
    dry_run: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def idem(self) -> Idempotency:
        return Idempotency(self.svc.sm)


JobFn = Callable[[JobContext], Awaitable[dict[str, Any]]]
JOBS: dict[str, JobFn] = {}


def job(name: str) -> Callable[[JobFn], JobFn]:
    def deco(fn: JobFn) -> JobFn:
        JOBS[name] = fn
        return fn

    return deco


def load_all() -> None:
    from app.jobs import (  # noqa: F401
        cleanup,
        duty,
        equipment,
        health,
        kb_sync,
        retention,
        web_crawl,
        weekly_report,
    )


async def run_job(
    svc: Services,
    name: str,
    *,
    today: date | None = None,
    dry_run: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    load_all()
    fn = JOBS.get(name)
    if fn is None:
        raise KeyError(f"Không có job '{name}'. Có: {', '.join(sorted(JOBS))}")
    ctx = JobContext(svc, today or clock.today(), dry_run, params or {})
    bind(job=name)
    t0 = time.monotonic()
    log.info("job_start", today=ctx.today.isoformat(), dry_run=dry_run)
    try:
        result = await fn(ctx)
    except DuplicateNames as e:
        metrics.JOB_RUNS.labels(name, "duplicate_names").inc()
        log.error("job_stopped_duplicate_names", error=str(e))
        if not dry_run:
            from app.jobs.alerts import alert_admins

            await alert_admins(
                svc, f"Job {name} dừng: Members có tên trùng", str(e), dedupe_key=f"dup:{name}:{ctx.today}"
            )
        return {"status": "stopped", "reason": str(e)}
    except Exception as e:
        metrics.JOB_RUNS.labels(name, "error").inc()
        log.exception("job_failed")
        if not dry_run:
            from app.jobs.alerts import alert_admins

            await alert_admins(
                svc,
                f"Job {name} lỗi",
                f"{type(e).__name__}: {str(e)[:1500]}",
                dedupe_key=f"jobfail:{name}:{ctx.today}",
            )
        raise
    metrics.JOB_RUNS.labels(name, "ok").inc()
    log.info("job_done", ms=int((time.monotonic() - t0) * 1000), result=_brief(result))
    return result


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if not isinstance(v, list) else len(v)) for k, v in result.items()}
