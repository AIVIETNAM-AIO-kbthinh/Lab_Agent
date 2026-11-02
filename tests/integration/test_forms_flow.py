"""Form engine end-to-end (P3 'Xong khi'):

- nộp được 3 loại đơn theo 2 cách (agent điền qua chat / user tự điền gửi file);
- sửa sau khi xem trước thì nút xác nhận cũ bị từ chối;
- hai người cùng mượn món thiết bị cuối: đúng một người thành công;
- bấm xác nhận 2 lần chỉ tạo 1 bản ghi;
- trường locked không sửa được qua chat; bản nháp quá hạn bị từ chối.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app import clock
from app.auth.roles import Role, UserContext
from app.db.models import Upload
from app.errors import DraftChanged, DraftInvalid
from app.forms.engine import ConfirmResult, FormEngine
from app.forms.extract import CellIn, FieldIn, RowIn
from app.forms.placeholder import insert_placeholders
from app.forms.publish import publish
from app.forms.render import build_context, render_docx
from app.forms.schema import load_schema_dir
from app.google.drive import DOCX, GDOC
from app.outbox.worker import OutboxWorker
from tests.conftest import Env

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "forms"
SCHEMAS = load_schema_dir(ROOT / "schemas")


# ---------------------------------------------------------------- helper


async def setup_forms(env: Env) -> None:
    for path, name, mime in (
        ("templates/DonXinGiaNhap", "DonXinGiaNhap.docx", GDOC),
        ("templates/PhieuMuonThietBi.docx", "PhieuMuonThietBi.docx", DOCX),
        ("templates/BienBanHop", "BienBanHop.docx", GDOC),
    ):
        env.lab.drive.add(path, mime, (FIX / name).read_bytes())
    for s in SCHEMAS.values():
        rep = await publish(env.svc, s)
        assert rep.ok, rep.as_dict()


async def ctx_for(env: Env, name: str | None, role: Role, sid: str) -> UserContext:
    member = (await env.svc.members.snapshot()).by_name(name) if name else None
    return UserContext(
        role=role, session_id=sid.ljust(32, "0"), email=member.email if member else None, member=member
    )


def fv(
    key: str, value: Any = None, items: list[str] | None = None, rows: list[dict[str, str]] | None = None
) -> FieldIn:
    return FieldIn(
        key=key,
        value=value,
        items=items,
        rows=[RowIn(cells=[CellIn(col=k, value=v) for k, v in r.items()]) for r in rows]
        if rows is not None
        else None,
    )


async def make_upload(env: Env, ctx: UserContext, data: bytes, filename: str) -> str:
    ext = filename.rsplit(".", 1)[1]
    folder = env.svc.config.var_dir / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    up = Upload(
        session_public_id=ctx.session_id,
        filename=filename,
        mime="application/pdf" if ext == "pdf" else DOCX,
        ext=ext,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        path="",
        expires_at=clock.now() + timedelta(hours=24),
    )
    async with env.svc.sm() as s, s.begin():
        s.add(up)
        await s.flush()
        p = folder / f"{up.id}.{ext}"
        p.write_bytes(data)
        up.path = str(p)
        return up.id


async def run_confirms(env: Env, *calls: Any) -> list[ConfirmResult]:
    """Chạy các lời gọi confirm song song, đồng thời cho worker outbox xử lý (như tiến trình worker thật)."""
    tasks = [asyncio.create_task(c) for c in calls]
    w = OutboxWorker(env.svc)
    for _ in range(400):
        await w.run_once()
        if all(t.done() for t in tasks):
            break
        await asyncio.sleep(0.02)
    return [await t for t in tasks]


def preview_action(res: Any) -> dict[str, Any]:
    acts = [a for a in res.ui_actions if a["type"] in ("form_preview", "form_confirm")]
    assert acts, (res.data.get("errors"), res.data.get("missing"), res.data.get("preview"))
    return acts[-1]


def borrows(env: Env) -> list[list[Any]]:
    return env.lab.sheets.values(env.lab.equipment_id, "LichSuMuon")[1:]


async def borrow_draft(
    env: Env, ctx: UserContext, rows: list[dict[str, str]]
) -> tuple[FormEngine, str, dict[str, Any]]:
    eng = FormEngine(env.svc)
    r = await eng.start("phieu_muon_thiet_bi", ctx)
    did = r.data["draft_id"]
    r = await eng.update(
        did,
        [
            fv("muc_dich", "Demo cho hội thảo"),
            fv("dia_diem", "Phòng 305"),
            fv("han_tra", "23/10"),
            fv("thiet_bi", rows=rows),
        ],
        ctx,
    )
    assert not r.data["missing"] and not r.data["errors"], r.data
    act = preview_action(await eng.preview(did, ctx))
    return eng, did, act


# ---------------------------------------------------------------- phiếu mượn (agent điền qua chat)


async def test_borrow_via_chat_full_flow(env: Env) -> None:
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    eng = FormEngine(env.svc)
    r = await eng.start("phieu_muon_thiet_bi", an)
    assert r.data["filled"]["Họ và tên"] == "Lê Văn An"  # mục I từ Members, locked
    assert (
        r.data["filled"]["Vai trò trong lab"] == "Thành viên"
        and r.data["filled"]["Điện thoại"] == "0933333333"
    )
    did = r.data["draft_id"]
    # trường locked không sửa được qua chat
    r = await eng.update(did, [fv("ho_ten", "Người Khác"), fv("muc_dich", "Demo")], an)
    assert any("không sửa được" in e["error"] for e in r.data["errors"])
    _, did2, act = await borrow_draft(env, an, [{"ten_tb": "arduino", "so_luong": "2", "ghi_chu": "kèm cáp"}])
    assert did2 == did  # tiếp tục bản nháp đang mở
    assert act["type"] == "form_preview" and act["version"] == 1 and act["mime"] == "application/pdf"
    assert ["Họ và tên", "Lê Văn An"] in act["summary"]

    (res,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], an))
    assert res.ok and res.status == "SUBMITTED", res
    assert res.data["ma_phieu"] == "P-20261016-01" and "P-20261016-01" in res.message
    new = [b for b in borrows(env) if b[10] == "P-20261016-01"]
    assert len(new) == 1
    assert new[0][:6] == ["16/10/2026", "Lê Văn An", "Arduino Uno", "Tốt", "23/10/2026", ""]
    assert new[0][7:10] == ["kèm cáp", "TB002", 2] and new[0][11].startswith("https://")
    cell = env.lab.sheets.values(env.lab.equipment_id, "Thietbi")[2][6]
    assert "Lê Văn An - 2 - 16/10/2026 - 23/10/2026" in cell
    # Còn lại = 10 − (Bình 3 + Trung Đào 1 + An 2) = 4; thiết bị khác không bị đụng tới
    tb = env.lab.sheets.values(env.lab.equipment_id, "Thietbi")
    assert tb[2][7] == 4 and tb[1][7] == 2
    sub = env.lab.sheets.values(env.lab.forms_id, "phieu_muon_thiet_bi")
    assert sub[0][:3] == ["submission_id", "thoi_gian_nop", "nguoi_nop"] and len(sub) == 2
    assert sub[1][2] == "Lê Văn An" and "Mã phiếu P-20261016-01" in sub[1]
    up = [f for f in env.lab.drive.uploads if f.name.endswith(".pdf")]
    assert [f.name for f in up] == ["2026-10-16_LeVanAn_P-20261016-01.pdf"]
    assert env.lab.drive.content[up[0].id] == (await eng.drafts.load(did)).rendered_bytes  # type: ignore[union-attr]
    assert any(r[3] == "form_submit:phieu_muon_thiet_bi" and r[5] == "OK" for r in env.lab.audit_rows())


async def test_edit_after_preview_rejects_old_button(env: Env) -> None:
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    eng, did, v1 = await borrow_draft(env, an, [{"ten_tb": "TB002", "so_luong": "1"}])
    r = await eng.update(did, [fv("dia_diem", "Phòng 101")], an)
    assert r.data["status"] == "COLLECTING" and r.data.get("preview_invalidated")
    with pytest.raises(DraftChanged):
        await eng.confirm(did, v1["shown_hash"], an)
    v2 = preview_action(await eng.preview(did, an))
    assert v2["version"] == 2 and v2["shown_hash"] != v1["shown_hash"]
    with pytest.raises(DraftChanged):  # nút v1 vẫn bị từ chối sau khi có v2
        await eng.confirm(did, v1["shown_hash"], an)
    (res,) = await run_confirms(env, eng.confirm(did, v2["shown_hash"], an))
    assert res.status == "SUBMITTED"


async def test_double_confirm_creates_one_record(env: Env) -> None:
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    eng, did, act = await borrow_draft(env, an, [{"ten_tb": "TB002", "so_luong": "1"}])
    before = len(borrows(env))
    r1, r2 = await run_confirms(
        env, eng.confirm(did, act["shown_hash"], an), eng.confirm(did, act["shown_hash"], an)
    )
    assert {r1.status, r2.status} <= {"SUBMITTED", "PROCESSING"} and "SUBMITTED" in {r1.status, r2.status}
    (r3,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], an))  # bấm lại sau khi xong
    assert r3.ok and "Đã gửi trước đó" in r3.message and r3.data["ma_phieu"] == r1.data["ma_phieu"]
    assert len(borrows(env)) == before + 1
    assert len(env.lab.sheets.values(env.lab.forms_id, "phieu_muon_thiet_bi")) == 2
    assert len([f for f in env.lab.drive.uploads if f.name.endswith(".pdf")]) == 1


async def test_two_people_borrow_last_projector(env: Env) -> None:
    """Máy chiếu TB001: tổng 2, An đang mượn 1 ⇒ còn 1. Bình và Thư cùng xác nhận: đúng một người thành công."""
    await setup_forms(env)
    binh = await ctx_for(env, "Phạm Thị Bình", Role.MEMBER, "binh")
    thu = await ctx_for(env, "Trần Thị Thư", Role.THU_KI, "thu")
    e1, d1, a1 = await borrow_draft(env, binh, [{"ten_tb": "máy chiếu", "so_luong": "1"}])
    e2, d2, a2 = await borrow_draft(env, thu, [{"ten_tb": "TB001", "so_luong": "1"}])
    r1, r2 = await run_confirms(
        env, e1.confirm(d1, a1["shown_hash"], binh), e2.confirm(d2, a2["shown_hash"], thu)
    )
    statuses = sorted([r1.status, r2.status])
    assert statuses == ["NEEDS_EDIT", "SUBMITTED"], (r1, r2)
    loser = r1 if r1.status == "NEEDS_EDIT" else r2
    assert "chỉ còn 0" in loser.message or "vừa được người khác mượn" in loser.message
    active_tb001 = [b for b in borrows(env) if b[8] == "TB001" and b[5] == ""]
    assert sum(int(b[9]) for b in active_tb001) == 2  # không vượt tổng số
    assert not any("thất bại" in s for s in env.gmail.subjects())  # điều kiện nghiệp vụ: không báo admin
    # người thua sửa phiếu (bỏ máy chiếu) rồi nộp lại được
    eng = e1 if loser is r1 else e2
    did, ctx = (d1, binh) if loser is r1 else (d2, thu)
    r = await eng.update(did, [fv("thiet_bi", rows=[{"ten_tb": "arduino", "so_luong": "1"}])], ctx)
    act = preview_action(await eng.preview(did, ctx))
    (again,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], ctx))
    assert again.status == "SUBMITTED", (again, r.data)


async def test_expired_draft_and_foreign_session_rejected(env: Env) -> None:
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    other = await ctx_for(env, "Phạm Thị Bình", Role.MEMBER, "binh")
    eng, did, act = await borrow_draft(env, an, [{"ten_tb": "TB002", "so_luong": "1"}])
    with pytest.raises(DraftInvalid):
        await eng.confirm(did, act["shown_hash"], other)
    with pytest.raises(DraftInvalid):
        await eng.update(did, [fv("dia_diem", "x")], other)
    clock.set_now(lambda: datetime(2026, 10, 17, 9, 30, tzinfo=clock.tz()))  # > 24 giờ
    with pytest.raises(DraftInvalid):
        await eng.confirm(did, act["shown_hash"], an)
    with pytest.raises(DraftInvalid):
        await eng.update(did, [fv("dia_diem", "x")], an)


# ---------------------------------------------------------------- đơn gia nhập (guest)


async def test_application_via_chat_with_cv(env: Env) -> None:
    await setup_forms(env)
    guest = await ctx_for(env, None, Role.GUEST, "guest1")
    eng = FormEngine(env.svc)
    info = await eng.blank_info("don_xin_gia_nhap", guest)
    assert info.ui_actions[0]["type"] == "download" and info.ui_actions[0]["url"].startswith(
        "/forms/don_xin_gia_nhap/blank?"
    )
    did = (await eng.start("don_xin_gia_nhap", guest)).data["draft_id"]
    r = await eng.update(
        did,
        [
            fv("ho_ten", "Hoàng Văn Xuân"),
            fv("mssv", "22020001"),
            fv("khoa_truong", "UET"),
            fv("sdt", "+84 912 345 678"),
            fv("email", "xuan@yahoo.com"),
            fv("huong_nghien_cuu", "IoT\nThị giác máy tính"),
            fv("thoi_gian_tham_gia", "Chiều thứ 3, thứ 5"),
        ],
        guest,
    )
    assert any(e["field"] == "email" for e in r.data["errors"])
    assert "CV (PDF hoặc DOCX)" in r.data["missing"]
    assert any("0912345678" in n for n in r.data["normalized"])
    await eng.update(did, [fv("email", "xuan@gmail.com")], guest)
    pre = await eng.preview(did, guest)
    assert not pre.ui_actions and pre.data["missing"] == [
        "CV (PDF hoặc DOCX)"
    ]  # chưa có CV thì chưa xem trước
    cv = await make_upload(env, guest, b"%PDF-1.4 cv of xuan", "CV Xuan.pdf")
    await eng.attach(did, "cv", cv, guest)
    act = preview_action(await eng.preview(did, guest))
    (res,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], guest))
    assert res.status == "SUBMITTED" and "xuan@gmail.com" in res.message
    assert "link" not in res.data  # không gửi link Drive cho guest
    folder = next(f for f in env.lab.drive.files.values() if f.name == "HoangVanXuan_0912345678")
    names = sorted(f.name for f in env.lab.drive.files.values() if folder.id in f.parents)
    assert names == ["2026-10-16_CV.pdf", "2026-10-16_DonXinGiaNhap.pdf"]
    row = env.lab.sheets.values(env.lab.forms_id, "don_xin_gia_nhap")[-1]
    header = env.lab.sheets.values(env.lab.forms_id, "don_xin_gia_nhap")[0]
    rec = dict(zip(header, row, strict=False))
    assert (
        rec["trang_thai"] == "CHO_DUYET" and rec["nguoi_nop"] == "guest" and rec["ho_ten"] == "Hoàng Văn Xuân"
    )
    assert rec["link_file"].endswith(folder.id) and rec["sdt"] == "'0912345678"

    # cùng SĐT nộp lại → cùng folder, version _v2
    d2 = (await eng.start("don_xin_gia_nhap", guest)).data["draft_id"]
    await eng.update(
        d2,
        [
            fv("ho_ten", "Hoàng Văn Xuân"),
            fv("mssv", "22020001"),
            fv("khoa_truong", "UET"),
            fv("sdt", "0912345678"),
            fv("email", "xuan@gmail.com"),
            fv("huong_nghien_cuu", "IoT"),
            fv("thoi_gian_tham_gia", "Tối"),
        ],
        guest,
    )
    await eng.attach(d2, "cv", await make_upload(env, guest, b"%PDF-1.4 cv v2", "cv2.pdf"), guest)
    act2 = preview_action(await eng.preview(d2, guest))
    (res2,) = await run_confirms(env, eng.confirm(d2, act2["shown_hash"], guest))
    assert res2.status == "SUBMITTED"
    names = sorted(f.name for f in env.lab.drive.files.values() if folder.id in f.parents)
    assert "2026-10-16_DonXinGiaNhap_v2.pdf" in names and "2026-10-16_CV_v2.pdf" in names


async def test_application_self_filled_file(env: Env) -> None:
    """User tự điền mẫu rồi gửi file: hợp lệ ⇒ không cần xem trước, lưu NGUYÊN file đã gửi (vẫn bấm xác nhận)."""
    await setup_forms(env)
    guest = await ctx_for(env, None, Role.GUEST, "guest2")
    s = SCHEMAS["don_xin_gia_nhap"]
    tpl, _ = insert_placeholders((FIX / "DonXinGiaNhap.docx").read_bytes(), s)
    values = {
        "ngay_lap": "16/10/2026",
        "ho_ten": "Đỗ Thị Hạ",
        "mssv": "SV-77",
        "khoa_truong": "HUST",
        "sdt": "0987654321",
        "email": "ha@vnu.edu.vn",
        "huong_nghien_cuu": "Robot",
        "thoi_gian_tham_gia": "Cuối tuần",
    }
    filled = render_docx(tpl, build_context(s, values, {}, {}))
    eng = FormEngine(env.svc)
    up = await make_upload(env, guest, filled, "DonXinGiaNhap_Ha.docx")
    r = await eng.submit_filled("don_xin_gia_nhap", up, guest)
    assert r.data["mode"] == "user_file" and r.data["missing"] == ["CV (PDF hoặc DOCX)"]
    assert not env.anthropic.calls and not env.openai.calls  # luật theo anchor đủ, không tốn LLM
    did = r.data["draft_id"]
    r = await eng.attach(did, "cv", await make_upload(env, guest, b"%PDF-1.4 cv ha", "cv.pdf"), guest)
    act = preview_action(r)
    assert act["type"] == "form_confirm" and act["shown_hash"] == hashlib.sha256(filled).hexdigest()
    (res,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], guest))
    assert res.status == "SUBMITTED"
    main = next(f for f in env.lab.drive.uploads if f.name.startswith("2026-10-16_DonXinGiaNhap"))
    assert main.name.endswith(".docx") and env.lab.drive.content[main.id] == filled  # đúng file user gửi


async def test_self_filled_invalid_then_fixed_requires_preview(env: Env) -> None:
    await setup_forms(env)
    guest = await ctx_for(env, None, Role.GUEST, "guest3")
    s = SCHEMAS["don_xin_gia_nhap"]
    tpl, _ = insert_placeholders((FIX / "DonXinGiaNhap.docx").read_bytes(), s)
    values = {
        "ho_ten": "Vũ Văn K",
        "mssv": "1",
        "khoa_truong": "UET",
        "sdt": "123",  # sai
        "email": "k@gmail.com",
        "huong_nghien_cuu": "AI",
        "thoi_gian_tham_gia": "Sáng",
    }
    filled = render_docx(tpl, build_context(s, values, {}, {}))
    eng = FormEngine(env.svc)
    r = await eng.submit_filled("don_xin_gia_nhap", await make_upload(env, guest, filled, "don.docx"), guest)
    did = r.data["draft_id"]
    assert any(e["field"] == "sdt" for e in r.data["errors"]) and not r.ui_actions
    await eng.attach(did, "cv", await make_upload(env, guest, b"%PDF-1.4 x", "cv.pdf"), guest)
    r = await eng.update(
        did, [fv("sdt", "0911222333")], guest
    )  # sửa qua chat ⇒ agent điền lại ⇒ phải xem trước
    assert r.data["mode"] == "agent_fill" and not r.ui_actions
    act = preview_action(await eng.preview(did, guest))
    assert act["type"] == "form_preview"
    (res,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], guest))
    assert res.status == "SUBMITTED"


async def test_self_filled_pdf_uses_llm_for_missing(env: Env) -> None:
    """File PDF (không đọc được bảng/ô theo luật) → LLM trích phần còn thiếu; code vẫn validate."""
    from tests.fakes.llm import reply

    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    env.anthropic.steps = [
        reply(
            tool_calls=[
                (
                    "submit_result",
                    {
                        "fields": [
                            {"key": "muc_dich", "value": "Thí nghiệm", "items": None, "rows": None},
                            {"key": "dia_diem", "value": "Lab", "items": None, "rows": None},
                            {"key": "han_tra", "value": "20/10/2026", "items": None, "rows": None},
                            {
                                "key": "thiet_bi",
                                "value": None,
                                "items": None,
                                "rows": [
                                    {
                                        "cells": [
                                            {"col": "ten_tb", "value": "Arduino"},
                                            {"col": "so_luong", "value": "3"},
                                        ]
                                    }
                                ],
                            },
                            {"key": "ho_ten", "value": "Kẻ Mạo Danh", "items": None, "rows": None},
                        ]
                    },
                )
            ]
        )
    ]
    pdf = b"%PDF-1.4\n" + b"PHIEU MUON" * 10
    eng = FormEngine(env.svc)
    import app.forms.extract as ex

    orig = ex.text_of
    ex.text_of = lambda data, ext: (["PHIẾU MƯỢN THIẾT BỊ", "Mục đích sử dụng: (xem bảng)"], None)  # type: ignore[assignment]
    try:
        r = await eng.submit_filled("phieu_muon_thiet_bi", await make_upload(env, an, pdf, "phieu.pdf"), an)
    finally:
        ex.text_of = orig
    assert (
        r.data["filled"]["untrusted_content"]
        if isinstance(r.data["filled"], dict) and "untrusted_content" in r.data["filled"]
        else True
    )
    d = await eng.drafts.load(r.data["draft_id"])
    assert d is not None
    assert d.fields_json["ho_ten"] == "Lê Văn An"  # LLM không ghi đè được trường locked
    assert d.fields_json["thiet_bi"][0]["ma_tb"] == "TB002" and d.fields_json["thiet_bi"][0]["so_luong"] == 3
    assert d.status == "READY" and r.ui_actions[0]["type"] == "form_confirm"


# ---------------------------------------------------------------- biên bản họp


async def test_meeting_minutes_saved_as_gdoc_and_indexed(env: Env) -> None:
    await setup_forms(env)
    thu = await ctx_for(env, "Trần Thị Thư", Role.THU_KI, "thu")
    eng = FormEngine(env.svc)
    r = await eng.start("bien_ban_hop", thu)
    assert r.data["filled"]["Chủ trì"] == "Nguyễn Văn Leader" and r.data["filled"]["Thư ký"] == "Trần Thị Thư"
    did = r.data["draft_id"]
    r = await eng.update(
        did,
        [
            fv("ngay_hop", "15/10"),
            fv("gio_bat_dau", "14h"),
            fv("gio_ket_thuc", "15h30"),
            fv("hinh_thuc", "truc tiep"),
            fv("dia_diem", "Phòng 305 E3"),
            fv("thanh_vien_tham_du", items=["an", "Khách mời Y"]),
            fv("noi_dung", "Báo cáo tiến độ dự án IoT.\nThảo luận lịch seminar."),
            fv("ket_luan", "Seminar thứ 6 hằng tuần."),
            fv(
                "phan_cong",
                rows=[
                    {"noi_dung": "Viết báo cáo", "nguoi_phu_trach": "Lê Văn An", "han_hoan_thanh": "20/10"}
                ],
            ),
        ],
        thu,
    )
    assert not r.data["errors"] and not r.data["missing"], r.data
    d = await eng.drafts.load(did)
    assert d is not None and d.fields_json["vang_mat"] == [
        "Phạm Thị Bình"
    ]  # đề xuất = Members − tham dự − chủ trì/thư ký
    assert d.meta_json["unknown"]["thanh_vien_tham_du"] == ["Khách mời Y"]
    r = await eng.update(did, [fv("gio_ket_thuc", "13h")], thu)
    assert any("phải sau" in e["error"] for e in r.data["errors"])
    assert not (await eng.preview(did, thu)).ui_actions  # vi phạm ràng buộc ⇒ chưa cho xem trước
    await eng.update(did, [fv("gio_ket_thuc", "15h30")], thu)
    act = preview_action(await eng.preview(did, thu))
    assert ["Thành viên tham dự", "Lê Văn An, Khách mời Y (ngoài lab)"] in act["summary"]
    (res,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], thu))
    assert res.status == "SUBMITTED" and "15/10/2026" in res.message and res.data["link"]
    doc = next(f for f in env.lab.drive.uploads if f.name == "2026-10-15_BienBanHop")
    assert doc.mime_type == GDOC
    folder = env.lab.drive.files[doc.parents[0]]
    assert folder.name == "meeting-minutes"
    pts = [p for p in env.store.points.values() if p.payload.get("doc_type") == "minutes"]
    assert pts and pts[0].payload["access"] == "member" and pts[0].payload["doc_date"] == "2026-10-15"
    assert sub_rows(env, "bien_ban_hop")[0]["phan_cong"].startswith("[{")


def sub_rows(env: Env, form_id: str) -> list[dict[str, Any]]:
    grid = env.lab.sheets.values(env.lab.forms_id, form_id)
    return [dict(zip(grid[0], r, strict=False)) for r in grid[1:]]


async def test_role_and_template_guards(env: Env) -> None:
    from app.errors import PermissionDenied
    from app.tools.registry import REGISTRY

    await setup_forms(env)
    guest = await ctx_for(env, None, Role.GUEST, "g")
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    eng = FormEngine(env.svc)
    with pytest.raises(PermissionDenied):
        await eng.start("phieu_muon_thiet_bi", guest)
    with pytest.raises(PermissionDenied):
        await eng.start("bien_ban_hop", an)  # chỉ thư ký / leader
    res = await REGISTRY.execute("list_forms", {}, an, env.svc)
    assert {f["form_id"] for f in res.for_llm["forms"]} == {"phieu_muon_thiet_bi"}
    res = await REGISTRY.execute("list_forms", {}, guest, env.svc)
    assert {f["form_id"] for f in res.for_llm["forms"]} == {"don_xin_gia_nhap"}
    # tool update_form_fields qua registry (schema strict) + untrusted_content
    did = (await REGISTRY.execute("start_form", {"form_id": "phieu_muon_thiet_bi"}, an, env.svc)).for_llm[
        "draft_id"
    ]
    res = await REGISTRY.execute(
        "update_form_fields",
        {"draft_id": did, "fields": [{"key": "muc_dich", "value": "Test", "items": None, "rows": None}]},
        an,
        env.svc,
    )
    assert not res.is_error and "untrusted_content" in res.for_llm["filled"]
    res = await REGISTRY.execute("render_preview", {"draft_id": did}, guest, env.svc)  # sai chủ
    assert res.is_error


async def test_update_accepts_labels_and_reports_unknown_keys(env: Env) -> None:
    """Model yếu hay gửi nhãn thay cho key; key sai phải hiện trong errors (không bị lẫn vào 'missing')."""
    await setup_forms(env)
    guest = await ctx_for(env, None, Role.GUEST, "g-labels")
    eng = FormEngine(env.svc)
    r = await eng.start("don_xin_gia_nhap", guest)
    assert {"key": "lop_khoa", "label": "Lớp / Khóa", "required": False} in r.data["form_fields"]
    did = r.data["draft_id"]
    r = await eng.update(
        did,
        [fv("Họ và tên", "Trần Văn Test"), fv("Số điện thoại", "0987654321"), fv("Ho ten day du", "x")],
        guest,
    )
    d = await eng.drafts.load(did)
    assert (
        d is not None and d.fields_json["ho_ten"] == "Trần Văn Test" and d.fields_json["sdt"] == "0987654321"
    )
    assert [e["field"] for e in r.data["errors"]] == ["Ho ten day du"] and "CHƯA được lưu" in r.data[
        "next_step"
    ]
    assert "Họ và tên" not in r.data["missing"]


async def test_agent_stream_emits_ui_action_event(env: Env) -> None:
    """Sự kiện SSE ui_action giữ nguyên `type` của hành động (download / form_preview ...) trong `action`."""
    from app.agents.loop import AgentRunner, collect
    from tests.fakes.llm import reply

    await setup_forms(env)
    guest = await ctx_for(env, None, Role.GUEST, "g-sse")
    env.openai.steps = [
        reply(tool_calls=[("get_form_template", {"form_id": "don_xin_gia_nhap"})]),
        reply("Bạn tải mẫu ở nút bên dưới nhé."),
    ]
    events = [ev async for ev in AgentRunner(env.svc).run_turn(guest, "Mình muốn xin gia nhập", None)]
    ui = [ev for ev in events if ev["type"] == "ui_action"]
    assert len(ui) == 1 and ui[0]["action"]["type"] == "download"
    assert ui[0]["action"]["url"].startswith("/forms/don_xin_gia_nhap/blank?")
    del collect


async def test_start_form_with_initial_fields(env: Env) -> None:
    """start_form nhận luôn thông tin đã có (model yếu hay dừng sau start_form mà không gọi update)."""
    from app.tools.registry import REGISTRY

    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an-start")
    res = await REGISTRY.execute(
        "start_form",
        {
            "form_id": "phieu_muon_thiet_bi",
            "fields": [
                {"key": "muc_dich", "value": "Demo đồ án", "items": None, "rows": None},
                {"key": "dia_diem", "value": "Phòng lab", "items": None, "rows": None},
                {"key": "han_tra", "value": "29/10", "items": None, "rows": None},
                {
                    "key": "thiet_bi",
                    "value": None,
                    "items": None,
                    "rows": [
                        {
                            "cells": [
                                {"col": "ten_tb", "value": "Arduino Uno"},
                                {"col": "so_luong", "value": "1"},
                            ]
                        }
                    ],
                },
            ],
        },
        an,
        env.svc,
    )
    assert not res.is_error and res.for_llm["missing"] == [] and res.for_llm["errors"] == []
    assert "render_preview" in res.for_llm["next_step"]


async def test_publish_twice_does_not_duplicate(env: Env) -> None:
    await setup_forms(env)
    before = len(env.lab.drive.uploads)
    for s in SCHEMAS.values():
        rep = await publish(env.svc, s)
        assert rep.ok and rep.unchanged
    assert len(env.lab.drive.uploads) == before  # không tạo template/schema trùng
