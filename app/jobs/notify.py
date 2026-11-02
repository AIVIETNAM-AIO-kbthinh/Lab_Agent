"""Gửi mail cá nhân từ job: email luôn lấy từ Members, mỗi khoá idempotent chỉ gửi một lần.

Tham số chạy tay (`labagent job run <job> --param k=v`):
- `only=Họ Tên[,Họ Tên]`: chỉ gửi cho những người này (để test).
- `resend=1`: gửi lại dù khoá đã gửi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.members import Member
from app.domain.names import norm_name, valid_email
from app.jobs.base import JobContext
from app.mail.mailer import OutgoingMail, Recipient

MAX_PREVIEWS = 3  # dry-run trả về nội dung vài mail đầu để xem trước


def truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def selected(ctx: JobContext, name: str) -> bool:
    only = str(ctx.params.get("only") or "").strip()
    if not only:
        return True
    return norm_name(name) in {norm_name(x) for x in only.split(",") if x.strip()}


@dataclass
class Delivery:
    sent: list[str] = field(default_factory=list)
    already_sent: list[str] = field(default_factory=list)
    no_email: list[str] = field(default_factory=list)  # Members thiếu email / email sai
    previews: list[dict[str, str]] = field(default_factory=list)

    def result(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sent": self.sent,
            "already_sent": self.already_sent,
            "no_email": self.no_email,
        }
        if self.previews:
            out["previews"] = self.previews
        return out


async def preview(ctx: JobContext, mail: OutgoingMail) -> dict[str, str]:
    """Mail đúng như sẽ gửi (đã thêm [TEST] / "Người nhận dự kiến" nếu đang test_mode)."""
    p = await ctx.svc.mailer.send(mail, dry_run=True)
    return {"to": ", ".join(p.to), "subject": p.subject, "body": p.body}


async def send_personal(
    ctx: JobContext,
    d: Delivery,
    member: Member,
    *,
    key: str,
    action: str,
    subject: str,
    body: str,
) -> None:
    svc = ctx.svc
    if not valid_email(member.email):
        d.no_email.append(member.ho_ten)
        if not ctx.dry_run:
            await svc.audit.log(
                "system", "system", action, member.ho_ten, False, "Members không có email hợp lệ"
            )
        return
    mail = OutgoingMail([Recipient(member.email, member.ho_ten)], subject, body)
    if ctx.dry_run:
        d.sent.append(member.ho_ten)
        if len(d.previews) < MAX_PREVIEWS:
            d.previews.append(await preview(ctx, mail))
        return
    if truthy(ctx.params.get("resend")):
        await ctx.idem.forget(key)
    if not await ctx.idem.claim(key, action):
        d.already_sent.append(member.ho_ten)
        return
    try:
        await svc.mailer.send(mail)
    except Exception:
        await ctx.idem.release(key)
        raise
    await ctx.idem.done(key)
    d.sent.append(member.ho_ten)
