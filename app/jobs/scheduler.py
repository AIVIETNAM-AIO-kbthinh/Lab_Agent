"""APScheduler trong tiến trình worker, múi giờ lab. Lịch đọc từ config.schedule (tên job → cron)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.jobs.base import JOBS, load_all, run_job
from app.logging import get_logger

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


def build_scheduler(svc: Services) -> AsyncIOScheduler:
    load_all()
    sched = AsyncIOScheduler(timezone=svc.config.tz)
    for name, cron in svc.config.schedule.items():
        if name not in JOBS:
            log.warning("schedule_unknown_job", job=name)
            continue
        trigger = CronTrigger.from_crontab(cron, timezone=svc.config.tz)

        async def _run(n: str = name) -> None:
            try:
                await run_job(svc, n)
            except Exception:  # noqa: S110 - đã log + alert trong run_job
                pass

        sched.add_job(
            _run,
            trigger,
            id=name,
            name=name,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
            replace_existing=True,
        )
        log.info("job_scheduled", job=name, cron=cron)
    return sched
