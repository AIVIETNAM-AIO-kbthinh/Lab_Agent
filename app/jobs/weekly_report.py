"""weekly_report (chủ nhật 20:00): mail tổng kết tuần, toàn bộ bằng template (TK §9.3).

Người nhận theo `notifications.weekly_report_to` (mặc định leader + thư ký), email tra trong Members lúc chạy.
Trước khi gửi: tự giao buổi trực T cho tab tuần tới (duty_assign; dry-run chỉ tính, không ghi).
Nội dung: lịch trực tuần tới (đã giao T) + ai chưa đủ buổi, ai đang mượn thiết bị gì, thiết bị đến hạn / quá hạn trả,
mượn–trả trong tuần, đơn mới, dữ liệu cần sửa.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.domain.dates import fmt_date, next_week, parse_sheet_date, week_of
from app.domain.duty import DutyTab, tab_link
from app.domain.equipment import Borrow, EquipmentSnapshot
from app.domain.members import Member, MembersSnapshot
from app.domain.names import norm_name, valid_email
from app.jobs.base import JobContext, job
from app.jobs.notify import preview, truthy
from app.mail.mailer import OutgoingMail, Recipient
from app.mail.templates import render

_EXT = re.compile(r"gia hạn[^;]*\(ngày (\d{1,2}/\d{1,2}/\d{4})\)", re.I)
GOOD = {"", "tốt", "tot", "binh thuong", "bình thường"}


def _in(d: date | None, start: date, end: date) -> bool:
    return d is not None and start <= d <= end


def _borrow_view(b: Borrow, eq: EquipmentSnapshot, today: date) -> dict[str, Any]:
    return {
        "ho_ten": b.ho_ten,
        "ten_tb": b.ten_tb,
        "ma_tb": eq.resolved_ma_tb(b),
        "so_luong": b.so_luong,
        "ngay_muon": fmt_date(b.ngay_muon, "%d/%m") or "?",
        "han_tra": fmt_date(b.han_tra, "%d/%m") or "?",
        "days_late": max(0, b.days_late(today)),
        "days_left": (b.han_tra - today).days if b.han_tra else None,
    }


def recipients(snap: MembersSnapshot, roles: Sequence[str]) -> list[Recipient]:
    people: list[Member | None] = []
    if "leader" in roles:
        people.append(snap.leader())
    if "thu_ki" in roles:
        people += snap.secretaries()
    seen: dict[str, Recipient] = {}
    for m in people:
        if m is not None and valid_email(m.email):
            seen.setdefault(m.email, Recipient(m.email, m.ho_ten))
    return list(seen.values())


async def build_report(
    ctx: JobContext, tab: DutyTab | None, assignment: dict[str, Any] | None = None
) -> dict[str, Any]:
    svc = ctx.svc
    cfg = svc.config.sheets.duty
    start, end = week_of(ctx.today)
    n_start, n_end = next_week(ctx.today)
    snap = await svc.members.snapshot(force=True)
    snap.assert_unique_names()
    names = snap.names()
    issues: list[str] = []

    # 1. lịch trực tuần tới
    under: list[dict[str, Any]] = []
    missing_rows: list[str] = []
    roster: list[dict[str, Any]] = []
    empty_slots: list[str] = []
    duty_link = None
    if tab is not None:
        duty_link = tab_link(await svc.duty.spreadsheet_id(), tab.sheet_id)
        roster = tab.roster(cfg.marks_counted, cfg.fixed_mark)
        empty_slots = [f"{d['label']} {sl['label']}" for d in roster for sl in d["slots"] if not sl["people"]]
        for r in tab.rows:
            c = tab.count_sessions(r, cfg.marks_counted)
            if c < cfg.min_sessions_per_week:
                under.append({"name": r.name, "count": c})
        in_tab = {r.key for r in tab.rows}
        missing_rows = [m.ho_ten for m in snap.active if m.key not in in_tab]
        bad = [r.name for r in tab.rows if r.key not in names]
        if bad:
            issues.append("Lịch trực tuần tới có tên không khớp Members: " + ", ".join(f'"{x}"' for x in bad))

    # 2–3. thiết bị
    eq = await svc.equipment.snapshot()
    today = ctx.today
    overdue = [
        _borrow_view(b, eq, today) for b in sorted(eq.overdue(today), key=lambda x: -x.days_late(today))
    ]
    # đến hạn trả từ hôm nay tới hết tuần tới (chưa quá hạn)
    due_soon = [
        _borrow_view(b, eq, today)
        for b in sorted(
            (b for b in eq.active_borrows() if b.han_tra is not None and today <= b.han_tra <= n_end),
            key=lambda x: (x.han_tra, norm_name(x.ho_ten)),
        )
    ]
    # ai đang mượn gì: gộp theo người, giữ thứ tự Members (tên lạ xếp cuối)
    order = {m.key: i for i, m in enumerate(snap.active)}
    holders: dict[str, dict[str, Any]] = {}
    for b in sorted(
        eq.active_borrows(),
        key=lambda x: (order.get(norm_name(x.ho_ten), len(order)), x.han_tra or date.max),
    ):
        h = holders.setdefault(norm_name(b.ho_ten), {"ho_ten": b.ho_ten, "items": []})
        h["items"].append(_borrow_view(b, eq, today))
    borrowed = sum(1 for b in eq.borrows if _in(b.ngay_muon, start, end))
    returned = [b for b in eq.borrows if _in(b.ngay_tra, start, end)]
    extended = 0
    for b in eq.borrows:
        for m in _EXT.finditer(b.ghi_chu or ""):
            if _in(parse_sheet_date(m.group(1)), start, end):
                extended += 1
    damaged = [
        {
            "ten_tb": b.ten_tb,
            "ma_tb": eq.resolved_ma_tb(b),
            "ho_ten": b.ho_ten,
            "tinh_trang": b.tinh_trang_khi_tra,
        }
        for b in returned
        if norm_name(b.tinh_trang_khi_tra) not in GOOD
    ]
    active_bad = Counter(b.ho_ten for b in eq.borrows if b.active and norm_name(b.ho_ten) not in names)
    for nm, cnt in active_bad.items():
        issues.append(f'LichSuMuon có tên không khớp Members: "{nm}" ({cnt} dòng đang mượn)')
    returned_bad = Counter(
        b.ho_ten
        for b in eq.borrows
        if not b.active and norm_name(b.ho_ten) not in names and snap.former_by_name(b.ho_ten) is None
    )
    for nm, cnt in returned_bad.items():
        key = f"warn_unmatched_borrow:{norm_name(nm)}"
        if ctx.dry_run or await ctx.idem.claim(key, "weekly_report_warn"):  # dòng đã trả chỉ cảnh báo một lần
            issues.append(f'LichSuMuon có tên không khớp Members: "{nm}" ({cnt} dòng, đã trả)')

    # 4–5. đơn
    apps = []
    others = []
    for form_id in await svc.submissions.form_tabs():
        subs = [x for x in await svc.submissions.list(form_id) if _in(x.submitted_on, start, end)]
        if form_id == "don_xin_gia_nhap":
            apps = [
                {
                    "ho_ten": x.text("ho_ten"),
                    "sdt": x.text("sdt"),
                    "email": x.text("email"),
                    "link": x.text("link_file"),
                }
                for x in subs
            ]
        elif subs:
            others.append({"form_id": form_id, "count": len(subs), "link": None})

    return {
        "lab_name": svc.config.lab.name,
        "week_label": f"{fmt_date(start, '%d/%m')} – {fmt_date(end)}",
        "next_week_label": f"{fmt_date(n_start, '%d/%m')} – {fmt_date(n_end, '%d/%m')}",
        "duty_link": duty_link,
        "duty_tab_exists": tab is not None,
        "assignment": assignment,
        "roster": roster,
        "empty_slots": empty_slots,
        "total_slots": len(tab.slots) if tab is not None else 0,
        "under_min": under,
        "missing_rows": missing_rows,
        "min_sessions": cfg.min_sessions_per_week,
        "overdue": overdue,
        "due_soon": due_soon,
        "due_until": fmt_date(n_end, "%d/%m"),
        "today_label": fmt_date(today, "%d/%m"),
        "holders": list(holders.values()),
        "active_count": sum(len(h["items"]) for h in holders.values()),
        "borrowed_count": borrowed,
        "returned_count": len(returned),
        "extended_count": extended,
        "damaged": damaged,
        "applications": apps,
        "other_forms": others,
        "data_issues": issues,
    }


@job("weekly_report")
async def weekly_report(ctx: JobContext) -> dict[str, Any]:
    """Khoá: tuần. `--param resend=1` để gửi lại báo cáo tuần đã gửi."""
    svc = ctx.svc
    start, _ = week_of(ctx.today)
    key = f"weekly_report:{start.isoformat()}"
    resend = truthy(ctx.params.get("resend"))
    if not ctx.dry_run and not resend and await ctx.idem.is_done(key):
        return {"status": "already_sent", "week": start.isoformat()}
    from app.jobs.duty import assign_week

    n_start, n_end = next_week(ctx.today)
    tab = await svc.duty.load_week(n_start, n_end)
    assignment = await assign_week(ctx, tab) if tab is not None else None
    data = await build_report(ctx, tab, assignment)
    subject, body = render("weekly_report", **data)
    snap = await svc.members.snapshot()
    roles = list(svc.config.notifications.weekly_report_to)
    to = recipients(snap, roles)
    if not to:
        from app.jobs.alerts import alert_admins

        if not ctx.dry_run:
            await alert_admins(
                svc,
                "Không gửi được báo cáo tuần",
                f"Members không có {' / '.join(roles)} với email hợp lệ.",
                dedupe_key=key,
            )
        return {"status": "no_recipient", "roles": roles, "subject": subject}
    mail = OutgoingMail(to, subject, body)
    if ctx.dry_run:
        return {
            "status": "dry_run",
            "to": [r.email for r in to],
            "subject": subject,
            "assignment": assignment,
            "previews": [await preview(ctx, mail)],
        }
    if resend:
        await ctx.idem.forget(key)
    if not await ctx.idem.claim(key, "weekly_report"):
        return {"status": "already_sent", "week": data["week_label"]}
    try:
        await svc.mailer.send(mail)
    except Exception:
        await ctx.idem.release(key)
        raise
    await ctx.idem.done(key)
    await svc.audit.log(
        "system",
        "system",
        "weekly_report",
        data["week_label"],
        True,
        {"to": len(to), "overdue": len(data["overdue"]), "issues": len(data["data_issues"])},
    )
    return {
        "status": "sent",
        "to": [f"{r.name} <{r.email}>" for r in to],
        "subject": subject,
        "assignment": assignment,
    }
