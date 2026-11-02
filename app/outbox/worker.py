"""Worker outbox: một bản duy nhất (advisory lock), lấy op bằng FOR UPDATE SKIP LOCKED, chạy tuần tự.

- Step xong ghi outbox_steps(op_id, step) + result_json; chạy lại bỏ qua step đã xong.
- Lỗi ⇒ RETRY với backoff mũ; quá số lần ⇒ FAILED + AuditLog FAILED + mail admin.
- Khởi động: op đang RUNNING (worker chết giữa chừng) được đưa về RETRY.
"""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from app import clock, metrics
from app.db.models import OutboxOp, OutboxStep
from app.logging import bind, get_logger
from app.outbox.steps import OPS, PreconditionFailed, StepContext

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


def backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(600, 5 * (2 ** max(0, attempt - 1))) + random.uniform(0, 2))  # noqa: S311


class OutboxWorker:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.max_attempts = svc.config.worker.outbox_max_attempts

    async def recover(self) -> int:
        async with self.svc.sm() as s, s.begin():
            res = await s.execute(
                update(OutboxOp)
                .where(OutboxOp.status == "RUNNING")
                .values(status="RETRY", next_attempt_at=clock.now())
            )
            n = int(getattr(res, "rowcount", 0) or 0)
        if n:
            log.warning("outbox_recovered", ops=n)
        return n

    async def _claim(self) -> OutboxOp | None:
        async with self.svc.sm() as s, s.begin():
            op = await s.scalar(
                select(OutboxOp)
                .where(OutboxOp.status.in_(("PENDING", "RETRY")), OutboxOp.next_attempt_at <= clock.now())
                .order_by(OutboxOp.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if op is None:
                return None
            op.status = "RUNNING"
            op.attempts += 1
            await s.flush()  # ghi RUNNING + attempts TRƯỚC khi tách object khỏi session
            s.expunge(op)
            return op

    async def run_once(self) -> bool:
        op = await self._claim()
        if op is None:
            return False
        await self.execute(op)
        return True

    async def execute(self, op: OutboxOp) -> None:
        bind(op_id=op.id)
        kind = OPS.get(op.kind)
        if kind is None:
            await self._fail(op, f"Loại op chưa đăng ký: {op.kind}", final=True)
            return
        async with self.svc.sm() as s:
            done = {
                r.step: r.result_json
                for r in (await s.scalars(select(OutboxStep).where(OutboxStep.op_id == op.id))).all()
            }
        ctx = StepContext(self.svc, op.id, op.kind, dict(op.payload_json), op.actor, dict(done))
        try:
            if kind.precheck is not None and not done:
                await kind.precheck(ctx)
            for name, fn in kind.steps:
                if name in done:
                    continue
                result = await fn(ctx)
                async with self.svc.sm() as s, s.begin():
                    s.add(OutboxStep(op_id=op.id, step=name, status="DONE", result_json=result))
                ctx.results[name] = result
                log.info("outbox_step_done", kind=op.kind, step=name)
        except PreconditionFailed as e:
            # điều kiện nghiệp vụ không còn thoả (vd thiết bị vừa bị mượn hết): không retry, không báo admin
            await self._fail(op, f"Precondition: {e}", final=True, alert=False)
            return
        except Exception as e:
            log.exception("outbox_step_failed", kind=op.kind, attempt=op.attempts)
            limit = kind.max_attempts or self.max_attempts
            await self._fail(op, f"{type(e).__name__}: {str(e)[:500]}", final=op.attempts >= limit)
            return
        async with self.svc.sm() as s, s.begin():
            await s.execute(
                update(OutboxOp)
                .where(OutboxOp.id == op.id)
                .values(status="DONE", result_json={"steps": list(ctx.results)}, last_error=None)
            )
        log.info("outbox_op_done", kind=op.kind)

    async def _fail(self, op: OutboxOp, error: str, *, final: bool, alert: bool = True) -> None:
        async with self.svc.sm() as s, s.begin():
            values: dict[str, Any] = {"last_error": error}
            if final:
                values["status"] = "FAILED"
            else:
                values["status"] = "RETRY"
                values["next_attempt_at"] = clock.now() + backoff(op.attempts)
            await s.execute(update(OutboxOp).where(OutboxOp.id == op.id).values(**values))
        if final:
            log.error("outbox_op_failed", kind=op.kind, error=error[:300])
            await self.svc.audit.log(
                op.actor, "system", f"outbox:{op.kind}", op.id, ok=False, detail=error[:1000]
            )
            if not alert:
                return
            from app.jobs.alerts import alert_admins

            await alert_admins(
                self.svc,
                f"Thao tác {op.kind} thất bại",
                f"Op {op.id} ({op.kind}) đã thất bại sau {op.attempts} lần thử.\nLỗi: {error[:1000]}",
                dedupe_key=f"outbox_failed:{op.id}",
            )

    async def refresh_gauges(self) -> None:
        async with self.svc.sm() as s:
            rows = (await s.execute(select(OutboxOp.status, func.count()).group_by(OutboxOp.status))).all()
        counts = {r[0]: int(r[1]) for r in rows}
        metrics.OUTBOX_BACKLOG.set(
            counts.get("PENDING", 0) + counts.get("RETRY", 0) + counts.get("RUNNING", 0)
        )
        metrics.OUTBOX_FAILED.set(counts.get("FAILED", 0))

    async def loop(self, stop: asyncio.Event) -> None:
        await self.recover()
        interval = self.svc.config.worker.poll_interval_s
        n = 0
        while not stop.is_set():
            try:
                busy = await self.run_once()
                n += 1
                if n % 30 == 0:
                    await self.refresh_gauges()
            except Exception:
                log.exception("outbox_loop_error")
                busy = False
            if not busy:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass


async def wait_for(svc: Services, op_id: str, timeout_s: float = 30) -> OutboxOp | None:
    """API chờ kết quả op bằng cách poll trạng thái (tối đa timeout_s)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        async with svc.sm() as s:
            op = await s.get(OutboxOp, op_id)
        if op is None or op.status in ("DONE", "FAILED") or asyncio.get_running_loop().time() >= deadline:
            return op
        await asyncio.sleep(0.5)
