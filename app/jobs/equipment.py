"""equipment_reminder (hằng ngày 08:00): nhắc hạn trả ngày mai; quá hạn nhắc mỗi N ngày. Gộp theo người mượn.

Khoá idempotent: họ tên + ma_phieu + ngày (TK §9).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from app.domain.dates import fmt_date
from app.domain.equipment import Borrow
from app.domain.names import norm_name
from app.jobs.base import JobContext, job
from app.mail.mailer import OutgoingMail, Recipient
from app.mail.templates import render


def _view(b: Borrow, ma_tb: str, today: Any) -> dict[str, Any]:
    return {
        "ten_tb": b.ten_tb,
        "ma_tb": ma_tb,
        "so_luong": b.so_luong,
        "ma_phieu": b.ma_phieu,
        "han_tra": fmt_date(b.han_tra),
        "days_late": b.days_late(today),
    }


@job("equipment_reminder")
async def equipment_reminder(ctx: JobContext) -> dict[str, Any]:
    svc = ctx.svc
    every = max(1, svc.config.equipment.overdue_reminder_every_days)
    snap = await svc.members.snapshot(force=True)
    snap.assert_unique_names()
    eq = await svc.equipment.snapshot()
    tomorrow = ctx.today + timedelta(days=1)
    due = eq.due_on(tomorrow)
    overdue = [b for b in eq.overdue(ctx.today) if (b.days_late(ctx.today) - 1) % every == 0]
    by_person: dict[str, dict[str, list[Borrow]]] = defaultdict(lambda: {"due": [], "overdue": []})
    for b in due:
        by_person[b.ho_ten]["due"].append(b)
    for b in overdue:
        by_person[b.ho_ten]["overdue"].append(b)

    sent, unmatched, skipped = [], [], []
    for name, groups in by_person.items():
        member = snap.by_name(name) or snap.former_by_name(name)
        adm = svc.config.auth.admin_by_name(name) if member is None else None  # admin mượn (C1)
        to = (
            Recipient(member.email, member.ho_ten)
            if member is not None and member.email
            else Recipient(adm.email, adm.ho_ten)
            if adm is not None
            else None
        )
        if to is None:
            unmatched.append(name)
            if not ctx.dry_run:
                await svc.audit.log(
                    "system", "system", "equipment_reminder", name, False, "không khớp Members"
                )
            continue
        items = groups["due"] + groups["overdue"]
        keys = {
            f"equip_remind:{norm_name(name)}:{b.ma_phieu or b.row_number}:{ctx.today.isoformat()}"
            for b in items
        }
        if ctx.dry_run:
            sent.append(name)
            continue
        fresh = [k for k in keys if await ctx.idem.claim(k, "equipment_reminder")]
        if not fresh:
            skipped.append(name)
            continue
        subject, body = render(
            "equipment_reminder",
            lab_name=svc.config.lab.name,
            name=to.name,
            tomorrow=fmt_date(tomorrow),
            due_tomorrow=[_view(b, eq.resolved_ma_tb(b), ctx.today) for b in groups["due"]],
            overdue=[_view(b, eq.resolved_ma_tb(b), ctx.today) for b in groups["overdue"]],
        )
        try:
            await svc.mailer.send(OutgoingMail([to], subject, body))
        except Exception:
            for k in fresh:
                await ctx.idem.release(k)
            raise
        for k in fresh:
            await ctx.idem.done(k)
        sent.append(name)
    if sent and not ctx.dry_run:
        await svc.audit.log(
            "system",
            "system",
            "equipment_reminder",
            ctx.today.isoformat(),
            True,
            {"sent": len(sent), "due": len(due), "overdue": len(overdue)},
        )
    return {
        "due_tomorrow": len(due),
        "overdue_notified": len(overdue),
        "sent": sent,
        "already_sent": skipped,
        "unmatched": unmatched,
        "dry_run": ctx.dry_run,
    }
