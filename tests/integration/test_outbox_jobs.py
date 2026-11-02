"""Outbox lỗi giữa chừng rồi chạy lại; job chạy 2 lần không tạo trùng."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text

from app.db.models import OutboxOp, OutboxStep
from app.jobs.base import run_job
from app.outbox.steps import OpKind, StepContext, enqueue, register
from app.outbox.worker import OutboxWorker
from tests.conftest import Env

pytestmark = pytest.mark.db
CALLS: dict[str, int] = {}


async def _s1(ctx: StepContext) -> dict[str, Any]:
    CALLS["s1"] = CALLS.get("s1", 0) + 1
    return {"uploaded": "file-1"}


async def _s2(ctx: StepContext) -> dict[str, Any]:
    CALLS["s2"] = CALLS.get("s2", 0) + 1
    if ctx.payload.get("fail_times", 0) >= CALLS["s2"]:
        raise RuntimeError("Google 500 giả lập")
    assert ctx.results["s1"] == {"uploaded": "file-1"}  # step sau thấy kết quả step trước
    return {"row": 12}


async def _s3(ctx: StepContext) -> None:
    CALLS["s3"] = CALLS.get("s3", 0) + 1


register(OpKind("test_three_steps", [("s1", _s1), ("s2", _s2), ("s3", _s3)]))


async def _ready_now(env: Env) -> None:
    async with env.svc.sm() as s, s.begin():
        await s.execute(text("UPDATE outbox_ops SET next_attempt_at = '2000-01-01'"))


async def test_outbox_resumes_after_failure(env: Env) -> None:
    CALLS.clear()
    async with env.svc.sm() as s, s.begin():
        op_id = await enqueue(s, "test_three_steps", {"fail_times": 1}, actor="tester")
    w = OutboxWorker(env.svc)
    assert await w.run_once()
    async with env.svc.sm() as s:
        op = await s.get(OutboxOp, op_id)
        steps = (await s.scalars(select(OutboxStep.step).where(OutboxStep.op_id == op_id))).all()
    assert op is not None and op.status == "RETRY" and list(steps) == ["s1"]
    assert not await w.run_once()  # chưa tới giờ retry
    await _ready_now(env)
    assert await w.run_once()
    async with env.svc.sm() as s:
        op = await s.get(OutboxOp, op_id)
    assert op is not None and op.status == "DONE"
    assert CALLS == {"s1": 1, "s2": 2, "s3": 1}  # s1 KHÔNG chạy lại


async def test_outbox_fails_after_max_attempts_and_alerts(env: Env) -> None:
    CALLS.clear()
    env.svc.config.worker.outbox_max_attempts = 2
    async with env.svc.sm() as s, s.begin():
        op_id = await enqueue(s, "test_three_steps", {"fail_times": 99})
    w = OutboxWorker(env.svc)
    await w.run_once()
    await _ready_now(env)
    await w.run_once()
    async with env.svc.sm() as s:
        op = await s.get(OutboxOp, op_id)
    assert op is not None and op.status == "FAILED" and "Google 500" in (op.last_error or "")
    assert any(r[5] == "FAILED" for r in env.lab.audit_rows())
    assert any("thất bại" in s for s in env.gmail.subjects())


async def test_enqueue_same_op_id_once_and_recover(env: Env) -> None:
    async with env.svc.sm() as s, s.begin():
        await enqueue(s, "test_three_steps", {}, op_id="fixed")
        await enqueue(s, "test_three_steps", {}, op_id="fixed")
        await s.execute(text("UPDATE outbox_ops SET status='RUNNING'"))
    async with env.svc.sm() as s:
        assert len((await s.scalars(select(OutboxOp))).all()) == 1
    assert await OutboxWorker(env.svc).recover() == 1


async def test_advisory_lock_single_worker(env: Env, engine) -> None:  # type: ignore[no-untyped-def]
    a, b = await engine.connect(), await engine.connect()
    try:
        assert (await a.execute(text("SELECT pg_try_advisory_lock(42001)"))).scalar() is True
        assert (await b.execute(text("SELECT pg_try_advisory_lock(42001)"))).scalar() is False
    finally:
        await a.execute(text("SELECT pg_advisory_unlock_all()"))
        await a.close()
        await b.close()


# ------------------------------------------------------------------ job


async def test_duty_create_and_remind_idempotent(env: Env) -> None:
    r1 = await run_job(env.svc, "duty_create")
    r2 = await run_job(env.svc, "duty_create")
    assert r1["status"] == "created" and r2["status"] == "exists"
    tab = env.lab.next_week_tab()
    assert sum(1 for t in env.lab.sheets.books[env.lab.duty_id] if t.title == tab) == 1
    # thêm một tên không có trong Members vào tab tuần sau
    env.lab.sheets.values(env.lab.duty_id, tab).append([5, "Người Lạ"])
    rem1 = await run_job(env.svc, "duty_remind", params={})
    assert len(rem1["sent"]) == 4 and rem1["unmatched"] == ["Người Lạ"]
    rem2 = await run_job(env.svc, "duty_remind")
    assert rem2["sent"] == [] and len(rem2["already_sent"]) == 4
    remind_mails = [s for s in env.gmail.subjects() if "lịch trực" in s.lower()]
    assert len(remind_mails) == 4 and all(s.startswith("[TEST]") for s in remind_mails)
    assert any(
        r[3] == "duty_remind" and r[5] == "FAILED" and "không khớp" in r[6] for r in env.lab.audit_rows()
    )


async def test_equipment_reminder_idempotent(env: Env) -> None:
    r1 = await run_job(env.svc, "equipment_reminder")
    assert sorted(r1["sent"]) == ["Lê Văn An", "Phạm Thị Bình"]
    r2 = await run_job(env.svc, "equipment_reminder")
    assert r2["sent"] == []
    bodies = env.gmail.bodies()
    assert len(bodies) == 2
    assert any("quá hạn" in b and "trễ 4 ngày" in b for b in bodies)
    assert any("ngày mai" in b and "Máy chiếu Epson (TB001)" in b for b in bodies)


async def test_weekly_report(env: Env) -> None:
    await run_job(env.svc, "duty_create")
    dry = await run_job(env.svc, "weekly_report", dry_run=True)
    body = dry["previews"][0]["body"]
    assert "Phạm Thị Bình — Arduino Uno (TB002) ×3" in body and "trễ 4 ngày" in body
    assert "Hoàng Văn E — 0912345678 — e@gmail.com" in body
    assert '"Trung Đào"' in body and "Hỏng nhẹ" in body
    assert "Chưa đủ 3 buổi (4 người)" in body
    assert sorted(dry["to"]) == ["leader@gmail.com", "thu@gmail.com"]
    assert env.gmail.sent == []  # dry-run không gửi
    assert (await run_job(env.svc, "weekly_report"))["status"] == "sent"
    assert (await run_job(env.svc, "weekly_report"))["status"] == "already_sent"
    assert len(env.gmail.sent) == 1


async def test_duplicate_names_stop_job(env: Env) -> None:
    env.lab.sheets.values(env.lab.members_id, "Members").append(
        [5, "X", "Lê Văn An", "", "", "", "", "x@gmail.com", "Member"]
    )
    res = await run_job(env.svc, "duty_create")
    assert res["status"] == "stopped"
    assert any("tên trùng" in s for s in env.gmail.subjects())


async def test_cleanup_jobs(env: Env) -> None:
    assert (await run_job(env.svc, "draft_cleanup"))["drafts_deleted"] == 0
    assert (await run_job(env.svc, "data_retention"))["conversations_deleted"] == 0
