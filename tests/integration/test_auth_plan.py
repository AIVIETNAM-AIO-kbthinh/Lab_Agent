"""authentication_plan.md đợt 1–2: C1 (admin mượn thiết bị), C2 (vòng đời tài khoản), C5 (leader quản lý mẫu
đơn), C9 (lịch trực qua chat), C4 (đẩy tài liệu KB), C6 (sửa dữ liệu + AuditLog)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.auth import accounts
from app.auth.roles import Role, UserContext
from app.db.models import Session as SessionRow
from app.errors import PermissionDenied, ValidationFailed
from app.main import create_app
from app.tools.registry import REGISTRY
from app.workflows import data_edit as wf_data
from app.workflows import duty as wf_duty
from tests.conftest import Env
from tests.integration.test_api import boot, h, make_account
from tests.integration.test_forms_flow import ctx_for, make_upload
from tests.integration.test_workflows import admin_ctx, borrows, confirm_action, devices

pytestmark = pytest.mark.db
PW = "Mat-Khau-Rat-Dai-2026"


@pytest.fixture
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(services=env.svc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def login(c: httpx.AsyncClient, env: Env, email: str, *, ready: bool = True) -> httpx.Response:
    await make_account(env, email, PW)
    if ready:
        async with env.svc.sm() as s, s.begin():
            acc = await accounts.get_by_email(s, email)
            assert acc is not None
            acc.must_change_password = False
    await boot(c)
    return await c.post("/auth/login", json={"email": email, "password": PW}, headers=h(c))


def drop_member(env: Env, name: str) -> None:
    grid = env.lab.sheets.values(env.lab.members_id, "Members")
    idx = next(i for i, r in enumerate(grid) if len(r) > 2 and r[2] == name)
    del grid[idx]
    env.svc.members.invalidate()


def duty_grid(env: Env) -> list[list[Any]]:
    return env.lab.sheets.values(env.lab.duty_id, "12/10-18/10")


# ---------------------------------------------------------------- C1: admin mượn thiết bị


async def test_admin_identity_and_tools(env: Env) -> None:
    admin = admin_ctx()
    assert admin.ho_ten == "Thầy Admin" and admin.member is None
    names = {t.name for t in REGISTRY.for_role(Role.ADMIN)}
    assert {"my_borrows", "return_equipment", "extend_borrow", "register_duty", "request_data_edit"} <= names
    res = await REGISTRY.execute("my_borrows", {}, admin, env.svc)
    assert not res.is_error and res.for_llm["phieu"] == []


async def test_admin_returns_own_borrow_and_audit_name(env: Env) -> None:
    # sổ mượn ghi theo HỌ TÊN admin trong config (admin không có trong Members)
    grid = env.lab.sheets.values(env.lab.equipment_id, "LichSuMuon")
    row = list(grid[1])
    row[1], row[5], row[6], row[10] = "Thầy Admin", "", "", "P-20261015-01"
    grid.append(row)
    admin = admin_ctx()
    res = await REGISTRY.execute("my_borrows", {}, admin, env.svc)
    assert res.for_llm["phieu"][0]["ma_phieu"] == "P-20261015-01"
    res = await REGISTRY.execute(
        "return_equipment",
        {
            "items": [
                {
                    "ma_phieu": "P-20261015-01",
                    "thiet_bi": None,
                    "so_luong": None,
                    "tinh_trang": "Tốt",
                    "ghi_chu": None,
                }
            ]
        },
        admin,
        env.svc,
    )
    assert not res.is_error, res.for_llm
    r = await confirm_action(env, res.for_llm["action_id"], admin)
    assert r.status == "DONE", r
    assert borrows(env)[-1][5]  # đã có ngày trả
    audit = env.lab.audit_rows()[-1]
    assert audit[3] == "equipment_return" and audit[2] == "admin" and "Thầy Admin" in audit[1]


# ---------------------------------------------------------------- C2: vòng đời tài khoản


async def test_login_rejected_when_not_in_members(client: httpx.AsyncClient, env: Env) -> None:
    r = await login(client, env, "cu@gmail.com")  # cựu thành viên: có tài khoản nhưng không còn trong Members
    assert r.status_code == 403 and "không còn hiệu lực" in r.json()["detail"]
    # sai mật khẩu vẫn là lỗi chung chung (không lộ email nào có tài khoản)
    r = await client.post("/auth/login", json={"email": "cu@gmail.com", "password": "sai"}, headers=h(client))
    assert r.status_code == 401


async def test_session_revoked_when_removed_from_members(client: httpx.AsyncClient, env: Env) -> None:
    assert (await login(client, env, "an@gmail.com")).status_code == 200
    assert (await client.get("/auth/me")).json()["role"] == "member"

    # Members lỗi tạm thời ⇒ guest tạm, KHÔNG huỷ session
    env.svc.members.invalidate()
    env.lab.sheets.fail_next = [RuntimeError("sheets down")] * 3
    me = (await client.get("/auth/me")).json()
    assert me["role"] == "guest" and me["notice"] is None
    env.lab.sheets.fail_next = []
    env.svc.members.invalidate()
    assert (await client.get("/auth/me")).json()["role"] == "member"

    # bị xoá khỏi Members ⇒ huỷ session ở request kế tiếp
    drop_member(env, "Lê Văn An")
    me = (await client.get("/auth/me")).json()
    assert me["role"] == "guest" and me["logged_in"] is False and "không còn hiệu lực" in me["notice"]
    async with env.svc.sm() as s:
        acc = await accounts.get_by_email(s, "an@gmail.com")
        assert acc is not None
        assert not (await accounts.sessions.sessions_of(s, acc.id))
    me = (await client.get("/auth/me")).json()
    assert me["role"] == "guest" and me["notice"] is None  # phiên khách mới


async def test_admin_account_management(client: httpx.AsyncClient, env: Env) -> None:
    assert (await login(client, env, "admin@gmail.com")).status_code == 200
    me = (await client.get("/auth/me")).json()
    assert me["role"] == "admin" and me["ho_ten"] == "Thầy Admin"
    r = await client.post("/admin/api/accounts", json={"email": "la@gmail.com"}, headers=h(client))
    assert r.status_code == 400  # email lạ: không có trong Members/admins
    r = await client.post("/admin/api/accounts", json={"email": "thu@gmail.com"}, headers=h(client))
    assert r.status_code == 200
    accs = {a["email"]: a for a in (await client.get("/admin/api/accounts")).json()["accounts"]}
    assert accs["thu@gmail.com"]["who"].startswith("Trần Thị Thư") and accs["admin@gmail.com"]["valid"]

    # thư ký đăng nhập ở client khác rồi bị admin xoá tài khoản ⇒ mọi session bị huỷ
    async with env.svc.sm() as s, s.begin():
        thu = await accounts.get_by_email(s, "thu@gmail.com")
        assert thu is not None
        s.add(
            SessionRow(
                id_hash="x" * 64,
                public_id="p" * 32,
                account_id=thu.id,
                guest=False,
                ip="",
                user_agent="",
                created_at=thu.created_at,
                last_seen_at=thu.created_at,
                expires_at=thu.created_at.replace(year=2099),
            )
        )
    r = await client.post(
        "/admin/api/accounts/action", json={"email": "thu@gmail.com", "action": "delete"}, headers=h(client)
    )
    assert r.status_code == 200
    async with env.svc.sm() as s:
        assert await accounts.get_by_email(s, "thu@gmail.com") is None
        assert await s.get(SessionRow, "x" * 64) is None
    assert any(row[3] == "account_delete" for row in env.lab.audit_rows())

    # AuditLog: chỉ xem
    rows = (await client.get("/admin/api/audit")).json()["rows"]
    assert rows and rows[0]["action"] == "account_delete"  # mới nhất trước
    assert (await client.get("/admin/api/audit?q=thu@gmail.com")).json()["count"] >= 1


# ---------------------------------------------------------------- C5: leader quản lý mẫu đơn


async def test_leader_templates_but_not_accounts(client: httpx.AsyncClient, env: Env) -> None:
    assert (await login(client, env, "leader@gmail.com")).status_code == 200
    assert (await client.get("/admin/api/templates")).status_code == 200
    assert (await client.get("/admin/api/accounts")).status_code == 403
    assert (await client.get("/admin/api/audit")).status_code == 403
    assert (await client.get("/admin/api/llm/costs")).status_code == 403
    names = {t.name for t in REGISTRY.for_role(Role.LEADER)}
    assert {"propose_template", "update_template_draft", "cancel_template_draft"} <= names
    assert "propose_template" not in {t.name for t in REGISTRY.for_role(Role.THU_KI)}


# ---------------------------------------------------------------- C9: lịch trực qua chat


async def test_register_own_duty(env: Env) -> None:
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    res = await REGISTRY.execute(
        "register_duty",
        {
            "action": "register",
            "slots": [{"thu": "Thứ 6", "buoi": "Chiều"}, {"thu": "Thứ 7", "buoi": "Sáng"}],
            "week": None,
        },
        an,
        env.svc,
    )
    assert not res.is_error, res.for_llm
    assert res.ui_actions[0]["type"] == "action_confirm"
    row = duty_grid(env)[8]
    assert row[1] == "Lê Văn An" and row[11] == ""  # chưa ghi gì trước khi bấm Xác nhận
    r = await confirm_action(env, res.for_llm["action_id"], an)
    assert r.status == "DONE", r
    row = duty_grid(env)[8]
    assert row[11] == "V" and row[12] == "V"  # T6 chiều (L), T7 sáng (M)
    assert any(a[3] == "duty_register" for a in env.lab.audit_rows())

    # buổi đã qua, ô "T" do lab giao: không sửa được qua chat
    with pytest.raises(ValidationFailed, match="đã qua"):
        await wf_duty.propose_register(env.svc, an, "register", [{"thu": "Thứ 2", "buoi": "Sáng"}], None)
    duty_grid(env)[8][14] = "T"  # CN sáng
    with pytest.raises(ValidationFailed, match="lab giao"):
        await wf_duty.propose_register(env.svc, an, "unregister", [{"thu": "Chủ nhật", "buoi": "Sáng"}], None)
    data, _ = await wf_duty.propose_register(
        env.svc, an, "unregister", [{"thu": "Thứ 7", "buoi": "Sáng"}], None
    )
    assert (await confirm_action(env, data["action_id"], an)).status == "DONE"
    assert duty_grid(env)[8][12] == ""


async def test_export_duty(env: Env) -> None:
    from app.exports.build import ExportRequest, build_table

    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    t = await build_table(env.svc, an, ExportRequest(dataset="duty"))
    assert {r["ho_ten"] for r in t.rows} == {"Nguyễn Văn Leader", "Trần Thị Thư", "Lê Văn An"}
    assert any(r["loai"] == "Lab giao" for r in t.rows)
    t = await build_table(env.svc, an, ExportRequest(dataset="duty", duty_member="Lê Văn An"))
    assert len(t.rows) == 1 and t.rows[0]["thu"] == "Thứ 3" and t.rows[0]["buoi"] == "Chiều"
    res = await REGISTRY.execute("export_data", {"dataset": "duty", "time_expr": "tuần sau"}, an, env.svc)
    assert not res.is_error and res.for_llm["so_dong"] == 0 and "Chưa có tab" in res.for_llm["luu_y"][0]


# ---------------------------------------------------------------- C4: đẩy tài liệu KB


async def test_secretary_publishes_public_doc(env: Env) -> None:
    from tests.integration.test_kb_sync import docx

    thu = await ctx_for(env, "Trần Thị Thư", Role.THU_KI, "thu")
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    up = await make_upload(env, thu, docx("Nội quy mới: tắt điều hoà khi ra về."), "NoiQuyMoi.docx")
    res = await REGISTRY.execute(
        "publish_kb_document",
        {"upload_id": up, "folder": "public", "title": None, "subfolder": None},
        an,
        env.svc,
    )
    assert res.is_error  # member không có tool này
    res = await REGISTRY.execute(
        "publish_kb_document",
        {"upload_id": up, "folder": "public", "title": None, "subfolder": None},
        thu,
        env.svc,
    )
    assert not res.is_error, res.for_llm
    r = await confirm_action(env, res.for_llm["action_id"], thu)
    assert r.status == "DONE", r
    files = [f for f in env.lab.drive.files.values() if f.name == "NoiQuyMoi.docx"]
    assert len(files) == 1
    guest = UserContext(Role.GUEST, "g".ljust(32, "0"))
    hits = await env.svc.retriever.search("điều hoà", guest.role)
    assert any("điều hoà" in h.text for h in hits.hits)
    # upload của session khác ⇒ không dùng được
    with pytest.raises(ValidationFailed):
        from app.workflows.kb import propose_publish

        await propose_publish(env.svc, admin_ctx("other"), up, "internal", None, None)


# ---------------------------------------------------------------- C6: sửa dữ liệu


async def test_leader_edits_device_but_not_members(env: Env) -> None:
    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    res = await REGISTRY.execute(
        "request_data_edit",
        {
            "target": "device",
            "row": "TB003",
            "changes": [
                {"column": "Trạng thái hoạt động", "value": "Tốt"},
                {"column": "ghi chu", "value": ""},
            ],
            "form_id": None,
            "thiet_bi": None,
            "week": None,
            "new_row": False,
            "former": False,
        },
        leader,
        env.svc,
    )
    assert not res.is_error, res.for_llm
    assert any("Hỏng → Tốt" in line for line in res.for_llm["summary"])
    r = await confirm_action(env, res.for_llm["action_id"], leader)
    assert r.status == "DONE", r
    tb3 = next(d for d in devices(env) if d[0] == "TB003")
    assert tb3[2] == "Tốt" and tb3[5] == ""
    assert any(a[3] == "data_edit" for a in env.lab.audit_rows())

    # cột hệ thống tự tính, Members, AuditLog: không sửa được
    with pytest.raises(ValidationFailed, match="tự tính"):
        await wf_data.propose_edit(
            env.svc, leader, target="device", row="TB001", changes=[{"column": "Còn lại", "value": "9"}]
        )
    with pytest.raises(PermissionDenied):
        await wf_data.propose_edit(
            env.svc, leader, target="member", row="Lê Văn An", changes=[{"column": "Lớp", "value": "K70"}]
        )
    thu = await ctx_for(env, "Trần Thị Thư", Role.THU_KI, "thu")
    with pytest.raises(PermissionDenied):
        await wf_data.propose_edit(
            env.svc, thu, target="device", row="TB001", changes=[{"column": "Số lượng", "value": "3"}]
        )
    assert "request_data_edit" not in {t.name for t in REGISTRY.for_role(Role.THU_KI)}

    # thêm thiết bị mới
    data, _ = await wf_data.propose_edit(
        env.svc,
        leader,
        target="device",
        row="TB009",
        new_row=True,
        changes=[{"column": "Thiết bị", "value": "Raspberry Pi 5"}, {"column": "Số lượng", "value": "4"}],
    )
    assert (await confirm_action(env, data["action_id"], leader)).status == "DONE"
    snap = await env.svc.equipment.snapshot()
    dev = snap.device("TB009")
    assert dev is not None and dev.tong_so_luong == 4


async def test_admin_edits_member_and_application_status_blocked(env: Env) -> None:
    admin = admin_ctx()
    data, _ = await wf_data.propose_edit(
        env.svc, admin, target="member", row="le van an", changes=[{"column": "Phone", "value": "0987654321"}]
    )
    r = await confirm_action(env, data["action_id"], admin)
    assert r.status == "DONE", r
    m = (await env.svc.members.snapshot(force=True)).by_name("Lê Văn An")
    assert m is not None and m.sdt.endswith("987654321")
    with pytest.raises(ValidationFailed, match="đổi vai trò"):
        await wf_data.propose_edit(
            env.svc, admin, target="member", row="Lê Văn An", changes=[{"column": "Role", "value": "Leader"}]
        )
    with pytest.raises(ValidationFailed, match="Address"):
        await wf_data.propose_edit(
            env.svc, admin, target="member", row="Lê Văn An", changes=[{"column": "Address", "value": "x"}]
        )
    with pytest.raises(ValidationFailed, match="duyệt"):
        await wf_data.propose_edit(
            env.svc,
            admin,
            target="submission",
            row="S1",
            form_id="don_xin_gia_nhap",
            changes=[{"column": "trang_thai", "value": "DA_DUYET"}],
        )


async def test_leader_edits_someone_duty_and_stale_value_rejected(env: Env) -> None:
    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    data, _ = await wf_data.propose_edit(
        env.svc, leader, target="duty", row="Phạm Thị Bình", changes=[{"column": "Thứ 7 Chiều", "value": "T"}]
    )
    # trước khi bấm Xác nhận có người khác sửa ô đó ⇒ không ghi đè
    duty_grid(env)[9] = [4, "Phạm Thị Bình", *([""] * 11), "v"]
    r = await confirm_action(env, data["action_id"], leader)
    assert r.status == "REJECTED" and "vừa bị sửa" in r.message
    duty_grid(env)[9] = [4, "Phạm Thị Bình"]
    data, _ = await wf_data.propose_edit(
        env.svc, leader, target="duty", row="Phạm Thị Bình", changes=[{"column": "Thứ 7 Chiều", "value": "T"}]
    )
    assert (await confirm_action(env, data["action_id"], leader)).status == "DONE"
    assert duty_grid(env)[9][13] == "T"
