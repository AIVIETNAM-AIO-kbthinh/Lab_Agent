"""Trả, trả một phần, gia hạn thiết bị (TK §8.3).

- Chỉ người mượn thao tác trên dòng của mình (khớp họ tên trong Members).
- Dòng cũ của lab (không có Mã phiếu) vẫn trả được: nhận diện theo (họ tên, tên thiết bị).
- Hỏng/mất: cập nhật "Trạng thái hoạt động" khi trả TOÀN BỘ số lượng của thiết bị; thiết bị nhiều chiếc mà chỉ
  vài chiếc hỏng thì ghi vào cột Ghi chú (không khoá cả lô) — báo cáo tuần liệt kê "trả kèm hư hỏng".
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING, Any

from app import clock
from app.auth.roles import UserContext
from app.domain.dates import fmt_date, parse_sheet_date
from app.domain.equipment import Borrow, EquipmentSnapshot
from app.domain.names import norm_name
from app.errors import ValidationFailed
from app.forms.validators import parse_date_input
from app.outbox.steps import OpKind, PreconditionFailed, StepContext, register
from app.workflows.actions import finish, propose

if TYPE_CHECKING:
    from app.services import Services

CONDITIONS = ["Tốt", "Hỏng nhẹ", "Hỏng", "Mất"]
DAMAGED = {"Hỏng nhẹ", "Hỏng", "Mất"}


def _match_rows(
    snap: EquipmentSnapshot, mine: list[Borrow], ma_phieu: str | None, thiet_bi: str | None
) -> tuple[list[Borrow], list[str]]:
    """Các dòng đang mượn khớp (phiếu, thiết bị). Trả (rows, lỗi/gợi ý)."""
    rows = mine
    if ma_phieu:
        rows = [b for b in rows if b.ma_phieu.upper() == ma_phieu.strip().upper()]
        if not rows:
            return [], [f"Bạn không có thiết bị nào đang mượn trong phiếu {ma_phieu}."]
    if thiet_bi:
        q = thiet_bi.strip()
        dev = snap.device(q)
        if dev is None:
            hits = snap.search(q, limit=5)
            devs = {d.ma_tb.upper() for d in hits}
            cand = [
                b
                for b in rows
                if snap.resolved_ma_tb(b).upper() in devs or norm_name(b.ten_tb) == norm_name(q)
            ]
        else:
            cand = [b for b in rows if snap.resolved_ma_tb(b).upper() == dev.ma_tb.upper()]
        if not cand:
            return [], [f"Không thấy '{thiet_bi}' trong các thiết bị bạn đang mượn."]
        kinds = {snap.resolved_ma_tb(b).upper() or norm_name(b.ten_tb) for b in cand}
        if len(kinds) > 1:
            return [], [f"'{thiet_bi}' khớp nhiều thiết bị: " + ", ".join(sorted({b.ten_tb for b in cand}))]
        rows = cand
    return rows, []


def _row_payload(snap: EquipmentSnapshot, b: Borrow) -> dict[str, Any]:
    return {
        "row_number": b.row_number,
        "ma_phieu": b.ma_phieu,
        "ma_tb": snap.resolved_ma_tb(b),
        "ten_tb": b.ten_tb,
        "so_luong_muon": b.so_luong,
        "ho_ten": b.ho_ten,
        "han_tra": fmt_date(b.han_tra),
    }


# ---------------------------------------------------------------- trả


async def propose_return(
    svc: Services, ctx: UserContext, items: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not ctx.ho_ten:
        raise ValidationFailed(user_message="Tài khoản của bạn không gắn với thành viên nào trong Members.")
    snap = await svc.equipment.snapshot()
    mine = snap.active_borrows(ctx.ho_ten)
    if not mine:
        raise ValidationFailed(user_message="Bạn không có thiết bị nào đang mượn.")
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    used: set[int] = set()
    for it in items:
        cond = it["tinh_trang"]
        rows, errs = _match_rows(snap, mine, it.get("ma_phieu"), it.get("thiet_bi"))
        if errs:
            errors.extend(errs)
            continue
        if not it.get("thiet_bi") and not it.get("ma_phieu"):
            errors.append("Cần cho biết mã phiếu hoặc tên thiết bị cần trả.")
            continue
        qty = it.get("so_luong")
        if qty is not None and len(rows) > 1 and len({r.ma_phieu for r in rows}) > 1:
            errors.append(
                f"{rows[0].ten_tb} đang mượn ở nhiều phiếu ({', '.join(sorted({r.ma_phieu or '(cũ)' for r in rows}))}); "
                "hãy cho biết mã phiếu."
            )
            continue
        remaining = qty
        for b in sorted(rows, key=lambda x: (x.ngay_muon or date.min, x.row_number)):
            if b.row_number in used:
                continue
            n = b.so_luong if remaining is None else min(remaining, b.so_luong)
            if n <= 0:
                break
            out.append(
                {
                    **_row_payload(snap, b),
                    "so_luong_tra": n,
                    "tinh_trang": cond,
                    "ghi_chu": it.get("ghi_chu") or "",
                }
            )
            used.add(b.row_number)
            if remaining is not None:
                remaining -= n
        if remaining:
            total = sum(b.so_luong for b in rows)
            errors.append(f"Bạn chỉ đang mượn {total} {rows[0].ten_tb}, không thể trả {qty}.")
            out = [x for x in out if x["row_number"] not in {b.row_number for b in rows}]
    if errors:
        raise ValidationFailed(user_message=" ".join(errors))
    if not out:
        raise ValidationFailed(user_message="Không có thiết bị nào để trả.")
    summary = [
        f"Trả {x['ten_tb']} ({x['ma_tb'] or 'không mã'}) ×{x['so_luong_tra']}"
        f"{'/' + str(x['so_luong_muon']) if x['so_luong_tra'] < x['so_luong_muon'] else ''}"
        f" — phiếu {x['ma_phieu'] or '(phiếu cũ)'} — tình trạng khi trả: {x['tinh_trang']}"
        + (f" ({x['ghi_chu']})" if x["ghi_chu"] else "")
        for x in out
    ]
    summary.append(f"Ngày trả: {fmt_date(clock.today())}")
    return await propose(
        svc,
        ctx,
        "equipment_return",
        {"ho_ten": ctx.ho_ten, "items": out, "ngay_tra": fmt_date(clock.today())},
        summary,
    )


async def _check_rows(ctx: StepContext) -> EquipmentSnapshot:
    snap = await ctx.svc.equipment.snapshot()
    for it in ctx.payload["items"]:
        b = next((x for x in snap.borrows if x.row_number == it["row_number"]), None)
        if (
            b is None
            or not b.active
            or norm_name(b.ho_ten) != norm_name(ctx.payload["ho_ten"])
            or b.so_luong != it["so_luong_muon"]
        ):
            raise PreconditionFailed(
                f"Dòng mượn {it['ten_tb']} đã thay đổi trên sổ, hãy kiểm tra lại rồi tạo yêu cầu mới."
            )
    return snap


async def _return_precheck(ctx: StepContext) -> dict[str, Any] | None:
    await _check_rows(ctx)
    return None


async def step_write_returns(ctx: StepContext) -> dict[str, Any]:
    day = parse_sheet_date(ctx.payload["ngay_tra"]) or clock.today()
    out = {}
    for it in ctx.payload["items"]:
        out[str(it["row_number"])] = await ctx.svc.equipment.return_row(
            it["row_number"], it["so_luong_tra"], day, it["tinh_trang"]
        )
    return out


async def step_device_status(ctx: StepContext) -> dict[str, Any]:
    snap = await ctx.svc.equipment.snapshot()
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in ctx.payload["items"]:
        if it["tinh_trang"] in DAMAGED and it["ma_tb"]:
            by[it["ma_tb"].upper()].append(it)
    changed = []
    for ma, its in by.items():
        dev = snap.device(ma)
        if dev is None:
            continue
        qty = sum(i["so_luong_tra"] for i in its)
        worst = max((i["tinh_trang"] for i in its), key=CONDITIONS.index)
        detail = "; ".join(i["ghi_chu"] for i in its if i["ghi_chu"])
        note = f"{ctx.payload['ngay_tra']}: {qty} chiếc {worst.lower()} khi trả ({ctx.payload['ho_ten']})" + (
            f" — {detail}" if detail else ""
        )
        new_status = worst if dev.tong_so_luong <= qty else None
        await ctx.svc.equipment.note_device(ma, note, new_status)
        changed.append(ma)
    return {"devices": changed}


async def step_refresh(ctx: StepContext) -> None:
    await ctx.svc.equipment.refresh_nguoi_muon([i["ma_tb"] for i in ctx.payload["items"] if i["ma_tb"]])


async def step_return_audit(ctx: StepContext) -> None:
    items = ctx.payload["items"]
    await ctx.svc.audit.log(
        ctx.actor,
        ctx.payload.get("requested_role", "member"),
        "equipment_return",
        ", ".join(f"{i['ma_phieu'] or i['ten_tb']}:{i['ma_tb']}×{i['so_luong_tra']}" for i in items),
        True,
        {"ho_ten": ctx.payload["ho_ten"], "tinh_trang": [i["tinh_trang"] for i in items]},
    )


async def step_return_finish(ctx: StepContext) -> dict[str, Any]:
    items = ctx.payload["items"]
    names = ", ".join(f"{i['ten_tb']} ×{i['so_luong_tra']}" for i in items)
    msg = f"Đã ghi nhận trả: {names} (ngày {ctx.payload['ngay_tra']})."
    if any(i["tinh_trang"] in DAMAGED for i in items):
        msg += " Tình trạng hư hỏng/mất đã được ghi lại để leader xử lý."
    return await finish(ctx.svc, ctx.payload["action_id"], msg)


register(
    OpKind(
        "action:equipment_return",
        [
            ("write_returns", step_write_returns),
            ("device_status", step_device_status),
            ("nguoi_muon", step_refresh),
            ("audit", step_return_audit),
            ("finish", step_return_finish),
        ],
        precheck=_return_precheck,
    )
)


# ---------------------------------------------------------------- gia hạn


async def propose_extend(
    svc: Services, ctx: UserContext, ma_phieu: str, han_tra_moi: str, thiet_bi: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not ctx.ho_ten:
        raise ValidationFailed(user_message="Tài khoản của bạn không gắn với thành viên nào trong Members.")
    today = clock.today()
    new = parse_date_input(han_tra_moi, today)
    if new is None:
        raise ValidationFailed(user_message="Hạn trả mới không hợp lệ, dùng dạng dd/mm/yyyy.")
    snap = await svc.equipment.snapshot()
    rows, errs = _match_rows(snap, snap.active_borrows(ctx.ho_ten), ma_phieu, thiet_bi)
    if errs:
        raise ValidationFailed(user_message=" ".join(errs))
    if new < today:
        raise ValidationFailed(user_message="Hạn trả mới không được trước hôm nay.")
    late = [b for b in rows if b.han_tra and new <= b.han_tra]
    if late:
        raise ValidationFailed(
            user_message=f"Hạn trả mới phải sau hạn hiện tại ({fmt_date(max(b.han_tra for b in late if b.han_tra))})."
        )
    items = [_row_payload(snap, b) for b in rows]
    summary = [
        f"Gia hạn {i['ten_tb']} ({i['ma_tb'] or 'không mã'}) ×{i['so_luong_muon']} — phiếu {i['ma_phieu'] or '(cũ)'}: "
        f"{i['han_tra']} → {fmt_date(new)}"
        for i in items
    ]
    return await propose(
        svc,
        ctx,
        "equipment_extend",
        {"ho_ten": ctx.ho_ten, "items": items, "han_tra_moi": fmt_date(new)},
        summary,
    )


async def _extend_precheck(ctx: StepContext) -> dict[str, Any] | None:
    snap = await ctx.svc.equipment.snapshot()
    for it in ctx.payload["items"]:
        b = next((x for x in snap.borrows if x.row_number == it["row_number"]), None)
        if b is None or not b.active or norm_name(b.ho_ten) != norm_name(ctx.payload["ho_ten"]):
            raise PreconditionFailed(f"Dòng mượn {it['ten_tb']} đã thay đổi trên sổ, hãy tạo yêu cầu mới.")
    return None


async def step_write_extend(ctx: StepContext) -> dict[str, Any]:
    new = parse_sheet_date(ctx.payload["han_tra_moi"])
    assert new is not None
    snap = await ctx.svc.equipment.snapshot()
    done = []
    for it in ctx.payload["items"]:
        b = next((x for x in snap.borrows if x.row_number == it["row_number"]), None)
        if b is None or b.han_tra == new:
            continue
        await ctx.svc.equipment.extend(b, new, clock.today())
        done.append(it["row_number"])
    return {"rows": done}


async def step_extend_audit(ctx: StepContext) -> None:
    await ctx.svc.audit.log(
        ctx.actor,
        ctx.payload.get("requested_role", "member"),
        "equipment_extend",
        ", ".join(f"{i['ma_phieu'] or i['ten_tb']}:{i['ma_tb']}" for i in ctx.payload["items"]),
        True,
        {"han_tra_moi": ctx.payload["han_tra_moi"]},
    )


async def step_extend_finish(ctx: StepContext) -> dict[str, Any]:
    names = ", ".join(i["ten_tb"] for i in ctx.payload["items"])
    return await finish(
        ctx.svc, ctx.payload["action_id"], f"Đã gia hạn {names} đến ngày {ctx.payload['han_tra_moi']}."
    )


register(
    OpKind(
        "action:equipment_extend",
        [
            ("write", step_write_extend),
            ("nguoi_muon", step_refresh),
            ("audit", step_extend_audit),
            ("finish", step_extend_finish),
        ],
        precheck=_extend_precheck,
    )
)
