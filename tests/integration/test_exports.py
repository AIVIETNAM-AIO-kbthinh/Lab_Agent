"""Xuất file theo yêu cầu: lọc thành viên / thiết bị / lượt mượn, phân quyền cột, chống công thức, tải file."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from openpyxl import load_workbook

from app import clock
from app.auth.roles import Role, UserContext
from app.db.models import Export
from app.exports import store
from app.exports.build import ExportRequest, build_table
from app.jobs.base import run_job
from app.main import create_app
from app.tools.registry import REGISTRY, ToolResult
from tests.conftest import Env
from tests.fakes.llm import reply
from tests.integration.test_forms_flow import ctx_for

pytestmark = pytest.mark.db


def req(**kw: Any) -> dict[str, Any]:
    """Tham số đầy đủ như LLM strict gửi (trường không dùng = null)."""
    base: dict[str, Any] = {k: None for k in ExportRequest.model_fields}
    base["format"] = "xlsx"
    return {**base, **kw}


async def run(env: Env, ctx: UserContext, **kw: Any) -> ToolResult:
    return await REGISTRY.execute("export_data", req(**kw), ctx, env.svc)


async def sheet_rows(env: Env, res: ToolResult) -> list[list[Any]]:
    assert not res.is_error, res.for_llm
    export_id = res.ui_actions[0]["url"].rsplit("/", 1)[-1]
    async with env.svc.sm() as s:
        e = await s.get(Export, export_id)
        assert e is not None
    wb = load_workbook(io.BytesIO(e.data))
    return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]


# ---------------------------------------------------------------- thành viên


async def test_members_export_columns_by_role(env: Env) -> None:
    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    res = await run(env, leader, dataset="members")
    assert res.for_llm["so_dong"] == 4 and res.ui_actions[0]["type"] == "download"
    assert res.ui_actions[0]["filename"].startswith("DanhSachThanhVien_16-10-2026")
    rows = await sheet_rows(env, res)
    assert rows[0] == ["STT", "Họ tên", "MSSV", "Vai trò", "Email", "Số điện thoại", "Lớp", "Trường"]
    assert rows[1][1] == "Nguyễn Văn Leader" and rows[1][5] == "0911111111"
    assert not any("Phố" in str(c) for r in rows for c in r)  # Address không bao giờ xuất
    assert "Ngõ Kín" not in json.dumps(res.for_llm, ensure_ascii=False)  # dữ liệu không trả cho LLM

    # member thường: không có SĐT, MSSV; không xuất được cựu thành viên
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    rows = await sheet_rows(env, await run(env, an, dataset="members"))
    assert "Số điện thoại" not in rows[0] and "MSSV" not in rows[0]
    res = await run(env, an, dataset="members", member_status="former")
    assert res.is_error and "cựu thành viên" in res.for_llm["error"]
    res = await run(env, an, dataset="members", columns=["ho_ten", "sdt"])
    assert any("Số điện thoại" in n for n in res.for_llm["luu_y"])


async def test_members_filters_and_former(env: Env) -> None:
    thu = await ctx_for(env, "Trần Thị Thư", Role.THU_KI, "thu")
    rows = await sheet_rows(env, await run(env, thu, dataset="members", lop="k67", columns=["ho_ten", "lop"]))
    assert rows == [["Họ tên", "Lớp"], ["Trần Thị Thư", "K67"], ["Lê Văn An", "K67"]]
    rows = await sheet_rows(env, await run(env, thu, dataset="members", roles=["member"], truong="hust"))
    assert [r[1] for r in rows[1:]] == ["Phạm Thị Bình"]
    rows = await sheet_rows(env, await run(env, thu, dataset="members", member_status="all"))
    assert rows[0][-1] == "Trạng thái" and rows[-1][1] == "Đỗ Văn Cũ" and rows[-1][-1] == "Cựu thành viên"
    res = await run(env, thu, dataset="members", columns=["ho_ten", "dia_chi"])
    assert res.is_error and "valid_columns" in res.for_llm
    res = await run(env, thu, dataset="members", borrower="An")
    assert res.is_error and "borrower" in res.for_llm["error"]  # tham số sai loại dữ liệu ⇒ báo, không lờ đi
    res = await run(env, thu, dataset="members", truong="MIT")
    assert not res.is_error and res.for_llm["so_dong"] == 0 and not res.ui_actions


# ---------------------------------------------------------------- thiết bị + lượt mượn


async def test_devices_and_borrows_export(env: Env) -> None:
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    rows = await sheet_rows(env, await run(env, an, dataset="devices", availability="not_borrowable"))
    assert [r[0] for r in rows[1:]] == ["TB003"]
    rows = await sheet_rows(env, await run(env, an, dataset="devices", availability="borrowed"))
    assert [r[0] for r in rows[1:]] == ["TB001", "TB002"]
    arduino = next(r for r in rows if r[0] == "TB002")
    assert (
        arduino[4] == 4 and arduino[5] == 6 and "Phạm Thị Bình (3" in arduino[6] and "QUÁ HẠN" in arduino[6]
    )

    # thiết bị đang được mượn: ai trong lab cũng xuất được
    res = await run(env, an, dataset="borrows")
    assert res.for_llm["so_dong"] == 3 and res.ui_actions[0]["filename"].startswith("ThietBiDangMuon_")
    rows = await sheet_rows(env, res)
    assert rows[0][:3] == ["Mã phiếu", "Người mượn", "Mã thiết bị"]
    assert [r[1] for r in rows[1:]] == ["Phạm Thị Bình", "Lê Văn An", "Trung Đào"]  # theo ngày mượn
    assert rows[1][5].date() == clock.today() - timedelta(days=10)  # ngày là ô ngày thật trong Excel

    rows = await sheet_rows(env, await run(env, an, dataset="borrows", borrow_status="overdue"))
    assert len(rows) == 2 and rows[1][1] == "Phạm Thị Bình" and rows[1][7] == "Quá hạn" and rows[1][8] == 4
    rows = await sheet_rows(env, await run(env, an, dataset="borrows", device="arduino"))
    assert {r[1] for r in rows[1:]} == {"Phạm Thị Bình", "Trung Đào"}

    # lịch sử đã trả: member chỉ thấy của mình; xin của người khác bị từ chối
    res = await run(env, an, dataset="borrows", borrow_status="all")
    rows = await sheet_rows(env, res)
    assert {r[1] for r in rows[1:]} == {"Lê Văn An"} and len(rows) == 3 and res.for_llm["luu_y"]
    res = await run(env, an, dataset="borrows", borrow_status="returned", borrower="Phạm Thị Bình")
    assert res.is_error

    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    rows = await sheet_rows(env, await run(env, leader, dataset="borrows", borrow_status="all"))
    assert len(rows) == 5
    rows = await sheet_rows(env, await run(env, leader, dataset="borrows", borrow_status="returned"))
    assert len(rows) == 2 and rows[1][8] == "Đã trả" and rows[1][2] == "TB001"  # dòng cũ: mã tra theo tên
    res = await run(env, leader, dataset="borrows", time_expr="không hiểu nổi")
    assert res.is_error

    # thành viên có thật nhưng chưa từng mượn: ra 0 dòng, không đoán sang người khác tên gần giống
    res = await run(env, leader, dataset="borrows", borrow_status="all", borrower="Nguyễn Văn Leader")
    assert not res.is_error and res.for_llm["so_dong"] == 0 and not res.ui_actions
    res = await run(env, an, dataset="borrows", borrow_status="all", borrower="Nguyễn Văn Leader")
    assert res.is_error and "chính mình" in res.for_llm["error"]


async def test_csv_and_formula_injection(env: Env) -> None:
    grid = env.lab.sheets.values(env.lab.equipment_id, "Thietbi")
    grid.append(["TB009", '=HYPERLINK("http://x","bấm")', "Tốt", 1, "", "+cmd", "", 1])
    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    res = await run(env, leader, dataset="devices", device="TB009", format="csv", filename="thiet bi/la")
    assert not res.is_error, res.for_llm
    assert res.ui_actions[0]["filename"] == "thiet bi_la.csv"
    async with env.svc.sm() as s:
        e = await s.get(Export, res.ui_actions[0]["url"].rsplit("/", 1)[-1])
        assert e is not None
    text = e.data.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "'+cmd" in text

    rows = await sheet_rows(env, await run(env, leader, dataset="devices", device="TB009"))
    assert rows[1][1].startswith("=HYPERLINK")  # giữ nguyên chữ nhưng là chuỗi, không thành công thức


async def test_guest_has_no_export_tool(env: Env) -> None:
    assert "export_data" not in {t.name for t in REGISTRY.for_role(Role.GUEST)}
    guest = UserContext(role=Role.GUEST, session_id="g" * 32)
    res = await run(env, guest, dataset="members")
    assert res.is_error and not res.ui_actions


async def test_build_table_direct(env: Env) -> None:
    admin = UserContext(role=Role.ADMIN, session_id="a" * 32, email="admin@gmail.com")
    t = await build_table(env.svc, admin, ExportRequest(dataset="devices", tinh_trang="hỏng"))
    assert [r["ma_tb"] for r in t.rows] == ["TB003"] and t.filters == {"Tình trạng": "hỏng"}


# ---------------------------------------------------------------- tải file qua API


@pytest.fixture
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(services=env.svc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def h(c: httpx.AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies.get("la_csrf") or ""}


async def test_chat_export_download_only_owner(client: httpx.AsyncClient, env: Env) -> None:
    await client.get("/auth/me")
    r = await client.post("/auth/dev-login", json={"name": "Trần Thị Thư"}, headers=h(client))
    assert r.status_code == 200
    call = reply(tool_calls=[("export_data", req(dataset="borrows", borrow_status="overdue"))])
    env.anthropic.steps = [call, reply("Đây là file thiết bị quá hạn.")]
    env.openai.steps = [call, reply("Đây là file thiết bị quá hạn.")]
    r = await client.post("/chat", json={"message": "xuất cho tôi file thiết bị quá hạn"}, headers=h(client))
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    actions = [e["action"] for e in events if e["type"] == "ui_action"]
    assert actions and actions[0]["type"] == "download", events
    url = actions[0]["url"]

    r = await client.get(url)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert (
        "attachment" in r.headers["content-disposition"]
        and "ThietBiQuaHan" in r.headers["content-disposition"]
    )
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Quá hạn", "Thông tin"]

    # phiên khác (khách) không tải được
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services=env.svc)), base_url="http://testserver"
    ) as other:
        await other.get("/auth/me")
        assert (await other.get(url)).status_code == 404

    # hết hạn ⇒ 404, job dọn dẹp xoá
    real_now = clock.now()
    clock.set_now(lambda: real_now + timedelta(minutes=env.svc.config.exports.ttl_minutes + 1))
    assert (await client.get(url)).status_code == 404
    out = await run_job(env.svc, "draft_cleanup")
    assert out["exports_deleted"] >= 1
    assert await store.load_owned(env.svc, url.rsplit("/", 1)[-1], UserContext(Role.THU_KI, "x" * 32)) is None
