"""Thay đổi thành viên (TK §8.5–8.7): duyệt / từ chối đơn gia nhập, cho rời lab, đổi leader/thư ký.

Chỉ admin (authentication_plan.md C8). Luôn qua yêu cầu chờ xác nhận + AuditLog. Có hiệu lực ngay vì mọi
job/role đọc lại Members mỗi lần. Duyệt đơn tự tạo tài khoản và gửi mật khẩu tạm (C2); cho rời lab thì tài khoản
hết hiệu lực ở request kế tiếp (email không còn trong Members).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app import clock
from app.auth.roles import Role, UserContext
from app.domain.dates import fmt_date
from app.domain.members import MembersSnapshot
from app.domain.names import fuzzy_match, norm_name, valid_email
from app.domain.submissions import Submission
from app.errors import DuplicateNames, PermissionDenied, ValidationFailed
from app.mail.mailer import OutgoingMail, Recipient
from app.mail.templates import render
from app.outbox.steps import OpKind, PreconditionFailed, StepContext, register
from app.workflows.actions import finish, propose

if TYPE_CHECKING:
    from app.services import Services

APP_FORM = "don_xin_gia_nhap"
ROLE_NAMES = {"leader": "Leader", "thu_ki": "Thư ký", "member": "Thành viên"}


def _resolve_member(snap: MembersSnapshot, name: str) -> str:
    hits = fuzzy_match(name, [m.ho_ten for m in snap.active], limit=5, cutoff=72)
    if not hits:
        raise ValidationFailed(
            user_message=f"Không tìm thấy '{name}' trong danh sách thành viên đang hoạt động."
        )
    top, score = hits[0]
    close = [h for h, s in hits if s >= score - 5]
    if score < 100 and len(close) > 1:
        raise ValidationFailed(user_message=f"'{name}' khớp nhiều người: {', '.join(close)}. Hãy chọn một.")
    if score < 85:
        raise ValidationFailed(
            user_message=f"Không chắc '{name}' là ai. Có phải: {', '.join(h for h, _ in hits[:3])}?"
        )
    return top


async def _application(svc: Services, submission_id: str) -> Submission:
    subs = await svc.submissions.list(APP_FORM)
    sub = next((s for s in subs if s.text("submission_id") == submission_id.strip()), None)
    if sub is None:
        raise ValidationFailed(user_message=f"Không tìm thấy đơn gia nhập '{submission_id}'.")
    return sub


async def _mail(svc: Services, template: str, to: list[Recipient], **ctx: Any) -> int:
    to = [r for r in to if valid_email(r.email)]
    if not to:
        return 0
    subject, body = render(template, lab_name=svc.config.lab.name, **ctx)
    await svc.mailer.send(OutgoingMail(to, subject, body))
    return len(to)


def _admins(svc: Services) -> list[Recipient]:
    return [Recipient(e) for e in svc.config.alert_recipients()]


# ---------------------------------------------------------------- duyệt đơn


async def propose_approve(
    svc: Services, ctx: UserContext, submission_id: str, ho_ten_moi: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    sub = await _application(svc, submission_id)
    if sub.text("trang_thai") != "CHO_DUYET":
        raise ValidationFailed(
            user_message=f"Đơn {submission_id} đang ở trạng thái {sub.text('trang_thai') or '(trống)'}."
        )
    name = (ho_ten_moi or sub.text("ho_ten")).strip()
    if not name:
        raise ValidationFailed(user_message="Đơn không có họ tên.")
    snap = await svc.members.snapshot(force=True)
    if snap.by_name(name):
        raise ValidationFailed(
            user_message=f"Members đã có người tên '{name}'. Hãy cho biết tên có phần phân biệt, "
            f"vd '{name} ({sub.text('khoa_truong') or 'trường'})'."
        )
    payload = {
        "submission_id": sub.text("submission_id"),
        "ho_ten": name,
        "mssv": sub.text("mssv"),
        "lop": sub.text("lop_khoa"),
        "sdt": sub.text("sdt").lstrip("'"),
        "truong": sub.text("khoa_truong"),
        "email": sub.text("email"),
    }
    summary = [
        f"Duyệt đơn {payload['submission_id']}: thêm {name} vào Members với vai trò Thành viên",
        f"MSSV {payload['mssv'] or '—'} · Lớp {payload['lop'] or '—'} · {payload['truong'] or '—'} · "
        f"SĐT {payload['sdt'] or '—'} · {payload['email'] or '—'}",
        f"Tạo tài khoản Lab Agent cho {payload['email'] or '(không email)'}, gửi mật khẩu tạm và mail chào mừng.",
    ]
    return await propose(svc, ctx, "member_approve", payload, summary)


async def _approve_precheck(ctx: StepContext) -> dict[str, Any] | None:
    p = ctx.payload
    sub = await _application(ctx.svc, p["submission_id"])
    if sub.text("trang_thai") != "CHO_DUYET":
        raise PreconditionFailed(f"Đơn {p['submission_id']} không còn ở trạng thái chờ duyệt.")
    if (await ctx.svc.members.snapshot(force=True)).by_name(p["ho_ten"]):
        raise PreconditionFailed(f"Members đã có người tên '{p['ho_ten']}', hãy dùng tên có phần phân biệt.")
    return None


async def step_append_member(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    values = {k: p[k] for k in ("mssv", "ho_ten", "lop", "sdt", "truong", "email")}
    values["role"] = next(k for k, v in ctx.svc.config.sheets.members.role_values.items() if v == "member")
    try:
        row = await ctx.svc.members.append_member(values)
    except DuplicateNames:
        snap = await ctx.svc.members.snapshot(force=True)
        m = snap.by_name(p["ho_ten"])
        if m and m.email.casefold() == p["email"].casefold():
            return {"row": m.row_number, "existed": True}  # đã thêm ở lần chạy trước
        raise
    return {"row": row}


async def step_app_status(ctx: StepContext, status: str, note: str) -> None:
    await ctx.svc.submissions.set_status(APP_FORM, ctx.payload["submission_id"], status, note)


async def step_approve_status(ctx: StepContext) -> None:
    by = ctx.payload.get("requested_by", "")
    await step_app_status(ctx, "DA_DUYET", f"Duyệt ngày {fmt_date(clock.today())} bởi {by}")


async def step_create_account(ctx: StepContext) -> dict[str, Any]:
    """Tạo tài khoản cho thành viên mới + gửi mật khẩu tạm. Chạy lại an toàn: tài khoản đã có mà chưa đăng nhập
    lần nào thì cấp lại mật khẩu tạm (lần trước có thể chưa kịp gửi); đã dùng rồi thì không đụng tới."""
    from app.auth import accounts
    from app.auth.account_mail import send_temp_password

    p = ctx.payload
    email = (p.get("email") or "").strip().lower()
    if not valid_email(email):
        return {"created": False, "reason": "email không hợp lệ"}
    async with ctx.svc.sm() as s, s.begin():
        acc = await accounts.get_by_email(s, email)
        if acc is None:
            acc, pw = await accounts.create_account(s, email)
            created = True
        elif acc.last_login_at is None and acc.must_change_password:
            pw, created = await accounts.reset_password(s, email), False
        else:
            return {"created": False, "existed": True, "mailed": False}
    mailed = await send_temp_password(ctx.svc, email, pw, name=p["ho_ten"])
    return {"created": created, "mailed": mailed}


async def step_approve_mail(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    acc = ctx.results.get("account") or {}
    n = await _mail(
        ctx.svc,
        "application_approved_admin",
        _admins(ctx.svc),
        approver=p.get("requested_by", ""),
        account_mailed=bool(acc.get("mailed")),
        account_existed=bool(acc.get("existed")),
        **{k: p[k] for k in ("ho_ten", "email", "mssv", "truong", "sdt", "submission_id")},
    )
    n += await _mail(ctx.svc, "application_welcome", [Recipient(p["email"], p["ho_ten"])], ho_ten=p["ho_ten"])
    return {"sent": n}


async def step_approve_finish(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    acc = ctx.results.get("account") or {}
    await ctx.svc.audit.log(
        ctx.actor,
        "admin",
        "member_approve",
        p["ho_ten"],
        True,
        {"submission_id": p["submission_id"], "account": acc},
    )
    if acc.get("mailed"):
        note = f"Đã tạo tài khoản và gửi mật khẩu tạm tới {p['email']}."
    elif acc.get("existed"):
        note = f"Email {p['email']} đã có tài khoản từ trước."
    else:
        note = f"Chưa gửi được mật khẩu tạm tới {p['email'] or '(không email)'}: vào trang Quản trị để đặt lại mật khẩu."
    return await finish(ctx.svc, p["action_id"], f"Đã duyệt đơn và thêm {p['ho_ten']} vào Members. {note}")


register(
    OpKind(
        "action:member_approve",
        [
            ("append_member", step_append_member),
            ("set_status", step_approve_status),
            ("account", step_create_account),
            ("mail", step_approve_mail),
            ("finish", step_approve_finish),
        ],
        precheck=_approve_precheck,
    )
)


# ---------------------------------------------------------------- từ chối


async def propose_reject(
    svc: Services, ctx: UserContext, submission_id: str, ly_do: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    sub = await _application(svc, submission_id)
    if sub.text("trang_thai") != "CHO_DUYET":
        raise ValidationFailed(
            user_message=f"Đơn {submission_id} đang ở trạng thái {sub.text('trang_thai') or '(trống)'}."
        )
    payload = {
        "submission_id": sub.text("submission_id"),
        "ho_ten": sub.text("ho_ten"),
        "email": sub.text("email"),
        "ly_do": (ly_do or "").strip()[:500],
    }
    summary = [
        f"Từ chối đơn {payload['submission_id']} của {payload['ho_ten']}",
        f"Lý do (gửi kèm mail báo ứng viên): {payload['ly_do'] or '(không ghi)'}",
    ]
    return await propose(svc, ctx, "member_reject", payload, summary)


async def _reject_precheck(ctx: StepContext) -> dict[str, Any] | None:
    sub = await _application(ctx.svc, ctx.payload["submission_id"])
    if sub.text("trang_thai") != "CHO_DUYET":
        raise PreconditionFailed(f"Đơn {ctx.payload['submission_id']} không còn ở trạng thái chờ duyệt.")
    return None


async def step_reject_status(ctx: StepContext) -> None:
    p = ctx.payload
    note = f"Từ chối ngày {fmt_date(clock.today())} bởi {p.get('requested_by', '')}" + (
        f": {p['ly_do']}" if p["ly_do"] else ""
    )
    await step_app_status(ctx, "TU_CHOI", note)


async def step_reject_mail(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    n = await _mail(
        ctx.svc,
        "application_rejected",
        [Recipient(p["email"], p["ho_ten"])],
        ho_ten=p["ho_ten"],
        ly_do=p["ly_do"],
    )
    return {"sent": n}


async def step_reject_finish(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    await ctx.svc.audit.log(
        ctx.actor, "admin", "member_reject", p["ho_ten"], True, {"submission_id": p["submission_id"]}
    )
    return await finish(
        ctx.svc, p["action_id"], f"Đã từ chối đơn của {p['ho_ten']} và gửi mail báo ứng viên."
    )


register(
    OpKind(
        "action:member_reject",
        [("set_status", step_reject_status), ("mail", step_reject_mail), ("finish", step_reject_finish)],
        precheck=_reject_precheck,
    )
)


# ---------------------------------------------------------------- rời lab


async def propose_remove(
    svc: Services, ctx: UserContext, ho_ten: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    snap = await svc.members.snapshot(force=True)
    name = _resolve_member(snap, ho_ten)
    m = snap.by_name(name)
    assert m is not None
    if m.role == "leader":
        raise ValidationFailed(
            user_message="Không thể cho leader rời lab khi chưa có leader mới. Admin cần đổi leader trước."
        )
    borrows = (await svc.equipment.snapshot()).active_borrows(name)
    if borrows:
        items = ", ".join(f"{b.ten_tb} ×{b.so_luong} ({b.ma_phieu or 'phiếu cũ'})" for b in borrows)
        raise ValidationFailed(
            user_message=f"{name} còn thiết bị đang mượn: {items}. Cần xử lý trả thiết bị trước."
        )
    payload = {"ho_ten": name, "email": m.email, "ngay_roi": fmt_date(clock.today())}
    summary = [
        f"Chuyển {name} ({ROLE_NAMES.get(m.role, m.role)}, {m.email or 'không email'}) sang tab 'Cựu thành viên'",
        f"Ngày rời: {payload['ngay_roi']}. Từ tuần sau {name} không còn trong lịch trực; tài khoản hết hiệu lực ngay.",
    ]
    return await propose(svc, ctx, "member_remove", payload, summary)


async def _remove_precheck(ctx: StepContext) -> dict[str, Any] | None:
    name = ctx.payload["ho_ten"]
    if (await ctx.svc.equipment.snapshot()).active_borrows(name):
        raise PreconditionFailed(f"{name} vừa có thiết bị đang mượn, cần trả trước khi rời lab.")
    if (await ctx.svc.members.snapshot(force=True)).by_name(name) is None:
        raise PreconditionFailed(f"{name} không còn trong danh sách thành viên đang hoạt động.")
    return None


async def step_move_former(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    snap = await ctx.svc.members.snapshot(force=True)
    if snap.by_name(p["ho_ten"]) is None and snap.former_by_name(p["ho_ten"]) is not None:
        return {"moved": False, "already": True}
    await ctx.svc.members.move_to_former(p["ho_ten"], clock.today())
    return {"moved": True}


async def step_remove_mail(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    n = await _mail(
        ctx.svc,
        "member_left_admin",
        _admins(ctx.svc),
        ho_ten=p["ho_ten"],
        email=p["email"],
        ngay_roi=p["ngay_roi"],
        by=p.get("requested_by", ""),
    )
    return {"sent": n}


async def step_remove_finish(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    await ctx.svc.audit.log(
        ctx.actor, "admin", "member_remove", p["ho_ten"], True, {"ngay_roi": p["ngay_roi"]}
    )
    return await finish(ctx.svc, p["action_id"], f"Đã chuyển {p['ho_ten']} sang danh sách cựu thành viên.")


register(
    OpKind(
        "action:member_remove",
        [("move", step_move_former), ("mail", step_remove_mail), ("finish", step_remove_finish)],
        precheck=_remove_precheck,
    )
)


# ---------------------------------------------------------------- đổi role (admin)


async def propose_set_role(
    svc: Services, ctx: UserContext, ho_ten: str, role: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if ctx.role != Role.ADMIN:
        raise PermissionDenied("chỉ admin đổi role")
    if role not in ROLE_NAMES:
        raise ValidationFailed(user_message="Vai trò phải là leader, thu_ki hoặc member.")
    snap = await svc.members.snapshot(force=True)
    name = _resolve_member(snap, ho_ten)
    m = snap.by_name(name)
    assert m is not None
    if m.role == role:
        raise ValidationFailed(user_message=f"{name} đã là {ROLE_NAMES[role]}.")
    changes: list[dict[str, str]] = [{"ho_ten": name, "from": m.role, "to": role}]
    if role == "leader":
        old = snap.leader()
        if old and old.ho_ten != name:
            changes.append({"ho_ten": old.ho_ten, "from": "leader", "to": "member"})  # luôn đúng 1 leader
    elif m.role == "leader":
        raise ValidationFailed(
            user_message="Lab phải có đúng 1 leader: hãy chỉ định leader mới (người đó sẽ thay vị trí)."
        )
    summary = [f"{c['ho_ten']}: {ROLE_NAMES[c['from']]} → {ROLE_NAMES[c['to']]}" for c in changes]
    return await propose(svc, ctx, "member_set_role", {"changes": changes}, summary)


async def step_write_roles(ctx: StepContext) -> dict[str, Any]:
    snap = await ctx.svc.members.snapshot(force=True)
    done = []
    for c in ctx.payload["changes"]:
        m = snap.by_name(c["ho_ten"])
        if m is None:
            raise PreconditionFailed(f"{c['ho_ten']} không còn trong Members.")
        if m.role != c["to"]:
            await ctx.svc.members.set_role(c["ho_ten"], c["to"])
            done.append(c["ho_ten"])
    return {"changed": done}


async def step_role_finish(ctx: StepContext) -> dict[str, Any]:
    changes = ctx.payload["changes"]
    await ctx.svc.audit.log(
        ctx.actor,
        "admin",
        "member_set_role",
        ", ".join(c["ho_ten"] for c in changes),
        True,
        {"changes": changes},
    )
    text = "; ".join(f"{c['ho_ten']} → {ROLE_NAMES[c['to']]}" for c in changes)
    return await finish(ctx.svc, ctx.payload["action_id"], f"Đã cập nhật vai trò: {text}. Có hiệu lực ngay.")


register(OpKind("action:member_set_role", [("write", step_write_roles), ("finish", step_role_finish)]))


__all__ = ["norm_name", "propose_approve", "propose_reject", "propose_remove", "propose_set_role"]
