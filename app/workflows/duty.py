"""Đăng ký / bỏ đăng ký buổi trực của CHÍNH MÌNH qua chat (authentication_plan.md C9).

- Chỉ sửa dòng của người hỏi (khớp họ tên trong tab tuần). Ô "T" do lab giao: không bỏ được qua chat.
- Ghi "V" vào ô trống khi đăng ký; chỉ xoá ô "V" khi bỏ đăng ký. Ô có giá trị khác thì không đụng tới.
- Tuần mặc định: tuần đang mở đăng ký = tuần sau nếu đã có tab (job thứ Sáu tạo), không thì tuần này.
- Buổi đã qua (ngày trước hôm nay) không sửa được.
Cách cũ (điền thẳng qua link Drive trong mail) giữ nguyên.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from app import clock
from app.auth.roles import UserContext
from app.domain.dates import fmt_date, next_week, resolve_time_expr, week_of
from app.domain.duty import DAY_NAMES, DutyTab, Slot, _day_index
from app.domain.names import norm_name
from app.errors import ValidationFailed
from app.outbox.steps import OpKind, PreconditionFailed, StepContext, register
from app.sheets.a1 import col_letter, quote_tab
from app.sheets.table import cell_str, norm_header
from app.workflows.actions import finish, propose

if TYPE_CHECKING:
    from app.services import Services


async def _open_tab(svc: Services, week: str | None) -> tuple[DutyTab, date]:
    today = clock.today()
    if week:
        rng = resolve_time_expr(week, today)
        if rng is None:
            raise ValidationFailed(user_message=f"Không hiểu tuần '{week}'.")
        start, end = week_of(rng[0])
        tab = await svc.duty.load_week(start, end)
    else:
        start, end = next_week(today)
        tab = await svc.duty.load_week(start, end)
        if tab is None:
            start, end = week_of(today)
            tab = await svc.duty.load_week(start, end)
    if tab is None:
        raise ValidationFailed(
            user_message=f"Chưa có tab lịch trực cho tuần {fmt_date(start, '%d/%m')} – {fmt_date(end, '%d/%m')}."
        )
    return tab, start


def _find_slot(tab: DutyTab, thu: str, buoi: str) -> Slot | None:
    day = _day_index(thu)
    if day is None:
        return None
    want = norm_header(buoi)
    return next((s for s in tab.slots if s.day == day and norm_header(s.label) == want), None)


async def propose_register(
    svc: Services, ctx: UserContext, action: str, slots: list[dict[str, str]], week: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not ctx.ho_ten:
        raise ValidationFailed(user_message="Tài khoản của bạn không gắn với thành viên nào trong Members.")
    tab, start = await _open_tab(svc, week)
    row = tab.row_by_name(ctx.ho_ten)
    if row is None:
        raise ValidationFailed(
            user_message=f"Không có tên {ctx.ho_ten} trong lịch trực tuần {tab.title}. Liên hệ thư ký để được thêm."
        )
    cfg = svc.config.sheets.duty
    mark = cfg.marks_counted[0] if cfg.marks_counted else "V"
    fixed = cfg.fixed_mark.casefold()
    today = clock.today()
    cells: list[dict[str, Any]] = []
    errors: list[str] = []
    skipped: list[str] = []
    seen: set[int] = set()
    for it in slots:
        s = _find_slot(tab, it.get("thu", ""), it.get("buoi", ""))
        label = f"{it.get('thu', '')} {it.get('buoi', '')}".strip()
        if s is None:
            errors.append(f"Không có buổi '{label}' trong lịch trực tuần {tab.title}.")
            continue
        if s.col in seen:
            continue
        seen.add(s.col)
        text = tab.slot_text(s)
        if start + timedelta(days=s.day) < today:
            errors.append(f"{text} đã qua, không sửa được.")
            continue
        cur = cell_str(row.marks.get(s.col)).strip()
        if action == "register":
            if cur.casefold() in {m.casefold() for m in cfg.marks_counted}:
                skipped.append(f"{text} (đã có: {cur})")
                continue
            if cur:
                errors.append(f"Ô {text} đang ghi '{cur}', không ghi đè được qua chat.")
                continue
            cells.append({"col": s.col, "slot": text, "before": "", "after": mark})
        else:
            if not cur:
                skipped.append(f"{text} (chưa đăng ký)")
                continue
            if cur.casefold() == fixed:
                errors.append(
                    f"{text} là buổi lab giao ({cfg.fixed_mark}), không bỏ được qua chat; liên hệ thư ký."
                )
                continue
            if cur.casefold() != mark.casefold():
                errors.append(f"Ô {text} đang ghi '{cur}', không xoá được qua chat.")
                continue
            cells.append({"col": s.col, "slot": text, "before": cur, "after": ""})
    if errors:
        raise ValidationFailed(user_message=" ".join(errors))
    if not cells:
        raise ValidationFailed(
            user_message="Không có buổi nào cần thay đổi" + (f": {', '.join(skipped)}." if skipped else ".")
        )
    verb = "Đăng ký trực" if action == "register" else "Bỏ đăng ký"
    summary = [f"{verb}: {c['slot']}" for c in cells]
    summary.append(f"Người trực: {row.name} · tab {tab.title}")
    if skipped:
        summary.append("Không đổi: " + ", ".join(skipped))
    payload = {
        "tab": tab.title,
        "row_number": row.row_number,
        "ho_ten": row.name,
        "action": action,
        "cells": cells,
    }
    return await propose(svc, ctx, "duty_register", payload, summary)


async def _live_row(ctx: StepContext) -> tuple[DutyTab, Any]:
    svc = ctx.svc
    props = next((t for t in await svc.duty.tabs() if t.title.strip() == ctx.payload["tab"]), None)
    if props is None:
        raise PreconditionFailed(f"Tab lịch trực {ctx.payload['tab']} không còn.")
    tab = await svc.duty.load_tab(props)
    row = next((r for r in tab.rows if r.row_number == ctx.payload["row_number"]), None)
    if row is None or norm_name(row.name) != norm_name(ctx.payload["ho_ten"]):
        raise PreconditionFailed("Lịch trực vừa bị sắp xếp lại, hãy tạo yêu cầu mới.")
    return tab, row


async def _precheck(ctx: StepContext) -> dict[str, Any] | None:
    _, row = await _live_row(ctx)
    for c in ctx.payload["cells"]:
        cur = cell_str(row.marks.get(c["col"])).strip()
        if cur.casefold() not in (c["before"].casefold(), c["after"].casefold()):
            raise PreconditionFailed(f"Ô {c['slot']} vừa được sửa thành '{cur}', hãy kiểm tra lại.")
    return None


async def step_write(ctx: StepContext) -> dict[str, Any]:
    sid = await ctx.svc.duty.spreadsheet_id()
    p = ctx.payload
    await ctx.svc.google.sheets.batch_update_values(
        sid,
        [
            (f"{quote_tab(p['tab'])}!{col_letter(c['col'])}{p['row_number']}", [[c["after"]]])
            for c in p["cells"]
        ],
    )
    return {"cells": len(p["cells"])}


async def step_audit(ctx: StepContext) -> None:
    p = ctx.payload
    await ctx.svc.audit.log(
        ctx.actor,
        p.get("requested_role", "member"),
        "duty_register" if p["action"] == "register" else "duty_unregister",
        f"{p['ho_ten']} · {p['tab']}",
        True,
        {"slots": [c["slot"] for c in p["cells"]]},
    )


async def step_finish(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    verb = "đăng ký trực" if p["action"] == "register" else "bỏ đăng ký"
    return await finish(
        ctx.svc, p["action_id"], f"Đã {verb}: {', '.join(c['slot'] for c in p['cells'])} (tab {p['tab']})."
    )


register(
    OpKind(
        "action:duty_register",
        [("write", step_write), ("audit", step_audit), ("finish", step_finish)],
        precheck=_precheck,
    )
)

__all__ = ["DAY_NAMES", "propose_register"]
