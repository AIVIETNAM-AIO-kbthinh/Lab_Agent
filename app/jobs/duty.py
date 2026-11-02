"""Lịch trực tuần sau (TK §9.1, §9.2). Email người nhận luôn tra theo họ tên trong Members.

- duty_create   (thứ 6 18:00): tạo tab tuần sau từ template.
- duty_announce (sau duty_create): mail mời MỌI thành viên đánh V vào các buổi có thể trực, kèm link tab.
- duty_remind   : nhắc người chưa đủ số buổi tối thiểu.
- duty_assign   : tự giao buổi trực chính thức (đổi V → T). weekly_report gọi bước này trước khi gửi báo cáo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.domain.dates import fmt_date, next_week, week_of
from app.domain.duty import DutyPlan, DutyTab, plan_duty, tab_link
from app.domain.members import MembersSnapshot
from app.domain.names import norm_name
from app.jobs.base import JobContext, job
from app.jobs.notify import Delivery, selected, send_personal, truthy
from app.mail.templates import render


@job("duty_create")
async def duty_create(ctx: JobContext) -> dict[str, Any]:
    svc = ctx.svc
    snap = await svc.members.snapshot(force=True)
    snap.assert_unique_names()
    start, end = next_week(ctx.today)
    # create_week_tab tự idempotent: tab tuần đã tồn tại thì dừng (khoá = tên tab tuần)
    cur_start, cur_end = week_of(ctx.today)
    # ô T tuần này chỉ chép sang khi bật copy_fixed_from_prev (mặc định T do duty_assign giao lại mỗi tuần)
    prev = (
        await svc.duty.load_week(cur_start, cur_end) if svc.config.sheets.duty.copy_fixed_from_prev else None
    )
    names = [m.ho_ten for m in snap.active]
    res = await svc.duty.create_week_tab(start, end, names, prev, dry_run=ctx.dry_run)
    out = {
        "status": "created" if res.created else "exists",
        "tab": res.tab,
        "members": res.members_written,
        "fixed_copied": res.fixed_copied,
        "rows_inserted": res.rows_inserted,
        "dry_run": ctx.dry_run,
    }
    if res.created and not ctx.dry_run:
        await svc.audit.log("system", "system", "duty_create", res.tab, True, out)
    return out


@dataclass
class _Week:
    start: date
    end: date
    snap: MembersSnapshot
    tab: DutyTab
    link: str
    label: str
    deadline: str


async def _next_week(ctx: JobContext) -> _Week | dict[str, Any]:
    """Members (đọc mới) + tab tuần sau; chưa có tab thì trả về dict kết quả "no_tab"."""
    svc = ctx.svc
    snap = await svc.members.snapshot(force=True)
    snap.assert_unique_names()
    start, end = next_week(ctx.today)
    tab = await svc.duty.load_week(start, end)
    if tab is None:
        return {
            "status": "no_tab",
            "week": svc.duty.name_for(start, end),
            "hint": "Chạy `labagent job run duty_create` trước",
        }
    link = tab_link(await svc.duty.spreadsheet_id(), tab.sheet_id)
    deadline = svc.config.notifications.duty_deadline.format(sunday=start - timedelta(days=1))
    label = f"{fmt_date(start, '%d/%m')} – {fmt_date(end, '%d/%m/%Y')}"
    return _Week(start, end, snap, tab, link, label, deadline)


async def _log_unmatched(ctx: JobContext, w: _Week, action: str) -> list[str]:
    """Dòng trong tab lịch trực có tên không khớp Members → không gửi được, ghi AuditLog FAILED."""
    bad = [r.name for r in w.tab.rows if w.snap.by_name(r.name) is None]
    if not ctx.dry_run:
        for name in bad:
            await ctx.svc.audit.log("system", "system", action, name, False, "không khớp Members")
    return bad


@job("duty_announce")
async def duty_announce(ctx: JobContext) -> dict[str, Any]:
    """Mời mọi thành viên đang hoạt động đăng ký lịch trực tuần sau. Khoá: tuần + họ tên chuẩn hoá."""
    svc = ctx.svc
    cfg = svc.config.sheets.duty
    w = await _next_week(ctx)
    if isinstance(w, dict):
        return w
    d = Delivery()
    for m in w.snap.active:
        if not selected(ctx, m.ho_ten):
            continue
        row = w.tab.row_by_name(m.ho_ten)
        fixed = [w.tab.slot_text(s) for s in w.tab.fixed_marks(row, cfg.fixed_mark)] if row else []
        subject, body = render(
            "duty_announce",
            lab_name=svc.config.lab.name,
            name=m.ho_ten,
            week_label=w.label,
            link=w.link,
            min_sessions=cfg.min_sessions_per_week,
            deadline=w.deadline,
            fixed=fixed,
            fixed_mark=cfg.fixed_mark,
            missing_row=row is None,
        )
        await send_personal(
            ctx,
            d,
            m,
            key=f"duty_announce:{w.start.isoformat()}:{norm_name(m.ho_ten)}",
            action="duty_announce",
            subject=subject,
            body=body,
        )
    unmatched = await _log_unmatched(ctx, w, "duty_announce")
    if d.sent and not ctx.dry_run:
        await svc.audit.log("system", "system", "duty_announce", w.tab.title, True, {"sent": len(d.sent)})
    return {"week": w.tab.title, "link": w.link, **d.result(), "unmatched": unmatched, "dry_run": ctx.dry_run}


@job("duty_remind")
async def duty_remind(ctx: JobContext) -> dict[str, Any]:
    """Nhắc thành viên chưa đủ số buổi ở tab tuần sau (kể cả người chưa có dòng trong tab).

    Mặc định nhắc MỘT lần/tuần (khoá: tuần + họ tên). Muốn nhắc đợt nữa: `--param round=2`.
    """
    svc = ctx.svc
    cfg = svc.config.sheets.duty
    w = await _next_week(ctx)
    if isinstance(w, dict):
        return w
    rnd = str(ctx.params.get("round") or "1").strip()
    prefix = f"duty_remind:{w.start.isoformat()}:" + ("" if rnd == "1" else f"r{rnd}:")
    d = Delivery()
    missing_rows: list[str] = []
    for m in w.snap.active:
        row = w.tab.row_by_name(m.ho_ten)
        slots = w.tab.marked_slots(row, cfg.marks_counted) if row else []
        if len(slots) >= cfg.min_sessions_per_week:
            continue
        if row is None:
            missing_rows.append(m.ho_ten)
        if not selected(ctx, m.ho_ten):
            continue
        subject, body = render(
            "duty_remind",
            lab_name=svc.config.lab.name,
            name=m.ho_ten,
            count=len(slots),
            slots=[w.tab.slot_text(s) for s in slots],
            min_sessions=cfg.min_sessions_per_week,
            link=w.link,
            week_label=w.label,
            deadline=w.deadline,
            missing_row=row is None,
        )
        await send_personal(
            ctx, d, m, key=prefix + norm_name(m.ho_ten), action="duty_remind", subject=subject, body=body
        )
    unmatched = await _log_unmatched(ctx, w, "duty_remind")
    if d.sent and not ctx.dry_run:
        await svc.audit.log(
            "system", "system", "duty_remind", w.tab.title, True, {"sent": len(d.sent), "round": rnd}
        )
    return {
        "week": w.tab.title,
        "round": rnd,
        **d.result(),
        "missing_rows": missing_rows,
        "unmatched": unmatched,
        "dry_run": ctx.dry_run,
        "resend": truthy(ctx.params.get("resend")),
    }


def plan_view(tab: DutyTab, plan: DutyPlan) -> dict[str, Any]:
    return {
        "assigned": [{"name": r.name, "slot": tab.slot_text(sl)} for r, sl in plan.assigned],
        "uncovered": [tab.slot_text(sl) for sl in plan.uncovered],
        "no_slot": plan.no_slot,
        "loads": [
            {"name": x.name, "registered": x.registered, "target": x.target, "t": x.t}
            for x in plan.loads
            if x.registered
        ],
    }


async def assign_week(ctx: JobContext, tab: DutyTab) -> dict[str, Any]:
    """Giao T cho tab (sửa `tab` tại chỗ); ghi sheet trừ khi dry-run. Chạy lại chỉ giao thêm phần còn thiếu."""
    svc = ctx.svc
    cfg = svc.config.sheets.duty
    plan = plan_duty(tab, cfg.marks_counted, cfg.fixed_mark, cfg.registrations_per_t)
    view = plan_view(tab, plan)
    if plan.assigned and not ctx.dry_run:
        await svc.duty.write_marks(tab, plan.assigned, cfg.fixed_mark)
        await svc.audit.log(
            "system",
            "system",
            "duty_assign",
            tab.title,
            True,
            {
                "assigned": len(plan.assigned),
                "uncovered": view["uncovered"],
                "no_slot": plan.no_slot,
            },
        )
    return view


@job("duty_assign")
async def duty_assign(ctx: JobContext) -> dict[str, Any]:
    """Đổi V → T ở tab tuần sau: mỗi buổi ≥1 T, mỗi người ≥1 T và nhiều buổi đăng ký thì nhiều T hơn.

    `--dry-run` để xem trước phương án.
    """
    svc = ctx.svc
    start, end = next_week(ctx.today)
    tab = await svc.duty.load_week(start, end)
    if tab is None:
        return {"status": "no_tab", "week": svc.duty.name_for(start, end)}
    view = await assign_week(ctx, tab)
    return {"week": tab.title, **view, "dry_run": ctx.dry_run}
