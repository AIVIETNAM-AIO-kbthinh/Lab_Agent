"""Tiến trình worker: advisory lock (chỉ MỘT bản chạy) → outbox + scheduler (crawl, index, job định kỳ)."""

from __future__ import annotations

import asyncio
import signal
import sys

from sqlalchemy import text

from app import clock
from app.config import get_loaded
from app.logging import get_logger, setup_logging
from app.outbox.worker import OutboxWorker
from app.services import Services, build_services

log = get_logger(__name__)


async def _acquire_lock(svc: Services, stop: asyncio.Event):  # type: ignore[no-untyped-def]
    """Giữ connection có advisory lock suốt vòng đời worker; bản thứ hai chờ ở đây."""
    assert svc.engine is not None
    key = svc.config.worker.advisory_lock_key
    while not stop.is_set():
        conn = await svc.engine.connect()
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
        await conn.commit()
        if got:
            log.info("worker_lock_acquired", key=key)
            return conn
        await conn.close()
        log.warning("worker_lock_busy_waiting", key=key)
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            pass
    return None


async def main() -> None:
    loaded = get_loaded()
    setup_logging(loaded.settings.log_level)
    svc = build_services(loaded, pool_size=3)
    clock.configure(svc.config.timezone)
    from app.db.migrate import assert_up_to_date

    await assert_up_to_date(svc.sm)
    import app.forms.submit
    import app.jobs.web_crawl
    import app.tools.builtin  # noqa: F401  (tool + op outbox của luồng trả/gia hạn, thành viên)
    from app.jobs.scheduler import build_scheduler

    stop = asyncio.Event()
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    lock_conn = await _acquire_lock(svc, stop)
    if lock_conn is None:
        return
    try:
        await svc.store.ensure()
    except Exception as e:
        log.error("qdrant_ensure_failed", error=f"{type(e).__name__}: {e}")
    sched = build_scheduler(svc)
    sched.start()
    outbox = OutboxWorker(svc)
    log.info("worker_started", lab=svc.config.lab.id)
    try:
        await outbox.loop(stop)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        sched.shutdown(wait=False)
        await lock_conn.close()
        await svc.aclose()
        log.info("worker_stopped")
