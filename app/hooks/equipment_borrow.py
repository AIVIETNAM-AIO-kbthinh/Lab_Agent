"""Hook `equipment_borrow` — phiếu mượn thiết bị (TK §8.2). Mặc định chấp thuận (member đã hỏi leader trước).

- prepare (endpoint): sinh `ma_phieu` = P-YYYYMMDD-NN, NN = max(sequence DB, mã lớn nhất trong ngày trên LichSuMuon) + 1.
- precheck (worker): đọc lại LichSuMuon, kiểm tra số còn lại của MỌI dòng. Worker chạy tuần tự nên hai người cùng
  mượn món cuối cùng: op đầu ghi xong, op sau thấy hết hàng ⇒ dừng, đúng một người thành công.
- steps: mỗi thiết bị một dòng LichSuMuon chung ma_phieu (bỏ qua dòng đã có), ghi lại cột "Người mượn".
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from app import clock
from app.db.models import Draft, Sequence
from app.domain.equipment import EquipmentSnapshot
from app.forms.opctx import load_draft
from app.forms.schema import FormSchema
from app.forms.validators import VContext, _rule_availability
from app.hooks.base import Hook, register_hook
from app.outbox.steps import PreconditionFailed, StepContext

if TYPE_CHECKING:
    from app.services import Services


async def next_ma_phieu(svc: Services, day: date, snap: EquipmentSnapshot | None) -> str:
    prefix = f"P-{day:%Y%m%d}-"
    max_sheet = 0
    for b in snap.borrows if snap else []:
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", b.ma_phieu or "")
        if m:
            max_sheet = max(max_sheet, int(m.group(1)))
    name = f"ma_phieu:{day:%Y%m%d}"
    async with svc.sm() as s, s.begin():
        base = insert(Sequence).values(name=name, value=max_sheet + 1)
        stmt = base.on_conflict_do_update(
            index_elements=["name"], set_={"value": func.greatest(Sequence.value + 1, max_sheet + 1)}
        ).returning(Sequence.value)
        n = int((await s.execute(stmt)).scalar_one())
    return f"{prefix}{n:02d}"


async def _prepare(svc: Services, schema: FormSchema, d: Draft, vc: VContext) -> dict[str, Any]:
    return {"ma_phieu": await next_ma_phieu(svc, clock.today(), vc.equipment)}


def merged_rows(d: Draft) -> list[dict[str, Any]]:
    """Gộp các dòng cùng ma_tb (một dòng LichSuMuon cho mỗi cặp ma_phieu + ma_tb)."""
    by: dict[str, dict[str, Any]] = {}
    for r in (d.fields_json or {}).get("thiet_bi") or []:
        ma = str(r.get("ma_tb") or "").upper()
        if not ma:
            continue
        if ma in by:
            by[ma]["so_luong"] += int(r.get("so_luong") or 0)
            if r.get("ghi_chu"):
                by[ma]["ghi_chu"] = "; ".join(x for x in (by[ma]["ghi_chu"], r["ghi_chu"]) if x)
        else:
            by[ma] = {
                "ma_tb": r.get("ma_tb"),
                "ten_tb": r.get("ten_tb"),
                "so_luong": int(r.get("so_luong") or 0),
                "tinh_trang": r.get("tinh_trang") or "",
                "ghi_chu": r.get("ghi_chu") or "",
            }
    return list(by.values())


async def _precheck(ctx: StepContext, schema: FormSchema, d: Draft) -> None:
    f = schema.field("thiet_bi")
    assert f is not None
    snap = await ctx.svc.equipment.snapshot()
    vc = VContext(today=clock.today(), allowed_domains=[], equipment=snap)
    issues = _rule_availability(f, (d.fields_json or {}).get("thiet_bi") or [], vc)
    if issues:
        raise PreconditionFailed(
            "Thiết bị vừa được người khác mượn: "
            + " ".join(i.message for i in issues)
            + " Hãy sửa phiếu rồi xác nhận lại."
        )


async def step_borrow_rows(ctx: StepContext) -> dict[str, Any]:
    d, _ = await load_draft(ctx)
    f = d.fields_json or {}
    ma_phieu = (d.result_json or {})["ma_phieu"]
    link = ctx.results["upload"]["link"]
    rows = [
        {
            "ngay_muon": f.get("ngay_muon", ""),
            "ho_ten": f.get("ho_ten", ""),
            "ten_tb": r["ten_tb"],
            "tinh_trang_khi_muon": r["tinh_trang"],
            "han_tra": f.get("han_tra", ""),
            "ngay_tra": "",
            "tinh_trang_khi_tra": "",
            "ghi_chu": r["ghi_chu"],
            "ma_tb": r["ma_tb"],
            "so_luong": r["so_luong"],
            "ma_phieu": ma_phieu,
            "link_phieu": link,
        }
        for r in merged_rows(d)
    ]
    written = await ctx.svc.equipment.append_borrow_rows(rows)
    return {"rows": written, "ma_tb": [r["ma_tb"] for r in rows]}


async def step_nguoi_muon(ctx: StepContext) -> None:
    await ctx.svc.equipment.refresh_nguoi_muon(ctx.results["borrow_rows"]["ma_tb"])


def _message(schema: FormSchema, d: Draft, results: dict[str, Any]) -> str:
    res = d.result_json or {}
    han = (d.fields_json or {}).get("han_tra", "")
    return f"Phiếu mượn đã được ghi nhận. Mã phiếu: {res.get('ma_phieu')}. Nhớ trả thiết bị trước ngày {han} nhé."


HOOK = register_hook(
    Hook(
        "equipment_borrow",
        prepare=_prepare,
        precheck=_precheck,
        steps=[("borrow_rows", step_borrow_rows), ("nguoi_muon", step_nguoi_muon)],
        message=_message,
    )
)
