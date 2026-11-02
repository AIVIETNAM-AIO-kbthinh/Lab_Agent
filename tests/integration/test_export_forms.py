"""Xuất đơn đã nộp đúng file đang lưu trên Drive (nhiều đơn → ZIP): lọc theo người/thời gian/mã, quyền."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from typing import Any

import pytest

from app import clock
from app.auth.roles import Role, UserContext
from app.db.models import Export
from app.tools.registry import REGISTRY, ToolResult
from tests.conftest import Env
from tests.fakes.render import FakePdf
from tests.integration.test_forms_flow import borrow_draft, ctx_for, run_confirms, setup_forms

pytestmark = pytest.mark.db


def req(**kw: Any) -> dict[str, Any]:
    return {
        "form_id": "phieu_muon_thiet_bi",
        "person": None,
        "time_expr": None,
        "code": None,
        **kw,
    }


async def run(env: Env, ctx: UserContext, **kw: Any) -> ToolResult:
    return await REGISTRY.execute("export_form_files", req(**kw), ctx, env.svc)


async def file_of(env: Env, res: ToolResult) -> Export:
    assert not res.is_error, res.for_llm
    async with env.svc.sm() as s:
        e = await s.get(Export, res.ui_actions[0]["url"].rsplit("/", 1)[-1])
    assert e is not None
    return e


def stored(env: Env, name: str) -> bytes:
    f = next(f for f in env.lab.drive.uploads if f.name == name)
    return env.lab.drive.content[f.id]


async def submit_borrow(env: Env, ctx: UserContext, rows: list[dict[str, str]]) -> str:
    eng, did, act = await borrow_draft(env, ctx, rows)
    (res,) = await run_confirms(env, eng.confirm(did, act["shown_hash"], ctx))
    assert res.status == "SUBMITTED", res
    return str(res.data["ma_phieu"])


async def test_export_borrow_forms_last_week_as_stored(env: Env) -> None:
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    p1 = await submit_borrow(env, an, [{"ten_tb": "arduino", "so_luong": "2", "ghi_chu": "kèm cáp"}])
    p2 = await submit_borrow(env, an, [{"ten_tb": "TB001", "so_luong": "1"}])
    binh = await ctx_for(env, "Phạm Thị Bình", Role.MEMBER, "binh")
    await submit_borrow(env, binh, [{"ten_tb": "TB002", "so_luong": "1"}])
    n1, n2 = f"2026-10-16_LeVanAn_{p1}.pdf", f"2026-10-16_LeVanAn_{p2}.pdf"

    # một tuần sau: "các đơn mượn thiết bị của An trong tuần trước"
    clock.set_now(lambda: datetime(2026, 10, 23, 9, 0, tzinfo=clock.tz()))
    thu = await ctx_for(env, "Trần Thị Thư", Role.THU_KI, "thu")
    res = await run(env, thu, person="le van an", time_expr="tuần trước")
    assert res.for_llm["so_don"] == 2 and res.for_llm["bo_loc"]["Người nộp"] == "Lê Văn An"
    assert "luu_y" not in res.for_llm
    e = await file_of(env, res)
    assert e.mime == "application/zip" and e.filename == "PhieuMuonThietBi_LeVanAn_23-10-2026.zip"
    with zipfile.ZipFile(io.BytesIO(e.data)) as z:
        assert z.namelist() == [n1, n2]  # đúng định dạng đang lưu (PDF), không đổi sang DOCX
        assert z.read(n1) == stored(env, n1) and z.read(n2) == stored(env, n2)

    # một phiếu theo mã → đúng 1 file, nguyên bytes trên Drive
    res = await run(env, thu, code=p2)
    e = await file_of(env, res)
    assert e.filename == n2 and e.mime == "application/pdf" and e.data == stored(env, n2)

    # tuần này chưa có phiếu nào
    res = await run(env, thu, time_expr="tuần này")
    assert not res.is_error and res.for_llm["so_don"] == 0 and not res.ui_actions


async def test_member_only_own_forms(env: Env) -> None:
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    await submit_borrow(env, an, [{"ten_tb": "TB002", "so_luong": "1"}])
    binh = await ctx_for(env, "Phạm Thị Bình", Role.MEMBER, "binh")
    p = await submit_borrow(env, binh, [{"ten_tb": "TB002", "so_luong": "1"}])

    res = await run(env, binh, person="Lê Văn An")
    assert res.is_error and "của chính mình" in res.for_llm["error"]
    res = await run(env, binh)  # không nói ai ⇒ chỉ đơn của mình
    e = await file_of(env, res)
    assert (
        res.for_llm["so_don"] == 1
        and e.filename == f"2026-10-16_PhamThiBinh_{p}.pdf"
        and res.for_llm["luu_y"]
    )

    res = await run(env, an, form_id="khong_co")
    assert res.is_error and "valid_form_ids" in res.for_llm


async def test_applications_staff_only_and_missing_drive_file(env: Env) -> None:
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    res = await run(env, an, form_id="don_xin_gia_nhap")
    assert res.is_error and "thư ký" in res.for_llm["error"]
    # C3: thư ký xem / xuất được đơn gia nhập
    thu = await ctx_for(env, "Trần Thị Thư", Role.THU_KI, "thu")
    res = await run(env, thu, form_id="don_xin_gia_nhap", person="Hoàng Văn E")
    assert not res.is_error and "S1" in res.for_llm["luu_y"][0]
    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    # dòng S1 không có file trên Drive ⇒ không dựng lại, báo rõ là thiếu file
    res = await run(env, leader, form_id="don_xin_gia_nhap", person="Hoàng Văn E")
    assert not res.is_error and res.for_llm["so_don"] == 0 and not res.ui_actions
    assert "S1" in res.for_llm["luu_y"][0] and "Drive" in res.for_llm["note"]

    # link trong sheet trỏ tới file không do hệ thống tải lên ⇒ không tải
    other = env.lab.drive.add("random/secret.pdf", "application/pdf", b"%PDF secret")
    grid = env.lab.sheets.values(env.lab.forms_id, "don_xin_gia_nhap")
    grid[1][6] = f"https://drive.google.com/file/d/{other.id}/view"
    res = await run(env, leader, form_id="don_xin_gia_nhap", person="Hoàng Văn E")
    assert res.for_llm["so_don"] == 0 and not res.ui_actions


async def test_docx_stored_on_drive_is_downloaded_as_is(env: Env) -> None:
    assert isinstance(env.svc.converter, FakePdf)
    env.svc.converter._available = False  # máy không có LibreOffice ⇒ phiếu lưu dạng DOCX
    await setup_forms(env)
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    p = await submit_borrow(env, an, [{"ten_tb": "TB002", "so_luong": "1"}])
    res = await run(env, an, person="Lê Văn An")
    e = await file_of(env, res)
    name = f"2026-10-16_LeVanAn_{p}.docx"
    assert e.filename == name and e.data == stored(env, name) and "luu_y" not in res.for_llm
