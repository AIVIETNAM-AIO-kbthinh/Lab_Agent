"""Trả / trả một phần / gia hạn thiết bị, thay đổi thành viên, job retention, endpoint xác nhận."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.auth.roles import Role, UserContext
from app.errors import PermissionDenied, ValidationFailed
from app.jobs.base import run_job
from app.outbox.worker import OutboxWorker
from app.tools.registry import REGISTRY
from app.workflows import actions
from app.workflows import equipment as wf_eq
from app.workflows import members as wf_mem
from tests.conftest import Env
from tests.integration.test_forms_flow import borrow_draft, ctx_for, setup_forms

pytestmark = pytest.mark.db


async def confirm_action(env: Env, action_id: str, ctx: UserContext) -> actions.ActionResult:
    task = asyncio.create_task(actions.confirm(env.svc, action_id, ctx))
    w = OutboxWorker(env.svc)
    for _ in range(300):
        await w.run_once()
        if task.done():
            break
        await asyncio.sleep(0.02)
    return await task


def borrows(env: Env) -> list[list[Any]]:
    return env.lab.sheets.values(env.lab.equipment_id, "LichSuMuon")[1:]


def devices(env: Env) -> list[list[Any]]:
    return env.lab.sheets.values(env.lab.equipment_id, "Thietbi")[1:]


# ---------------------------------------------------------------- trả / gia hạn


async def test_partial_return_with_damage_then_extend(env: Env) -> None:
    binh = await ctx_for(env, "Phạm Thị Bình", Role.MEMBER, "binh")
    # tool chỉ TẠO yêu cầu, chưa ghi gì
    res = await REGISTRY.execute(
        "return_equipment",
        {
            "items": [
                {
                    "ma_phieu": "P-20261002-01",
                    "thiet_bi": "arduino",
                    "so_luong": 1,
                    "tinh_trang": "Hỏng",
                    "ghi_chu": "gãy chân",
                }
            ]
        },
        binh,
        env.svc,
    )
    assert not res.is_error and res.ui_actions[0]["type"] == "action_confirm"
    assert "×1/3" in res.for_llm["summary"][0]
    before = [list(r) for r in borrows(env)]
    assert borrows(env) == before
    r = await confirm_action(env, res.for_llm["action_id"], binh)
    assert r.ok and r.status == "DONE" and "Arduino Uno ×1" in r.message
    rows = [b for b in borrows(env) if b[10] == "P-20261002-01"]
    returned = [b for b in rows if b[5]]
    active = [b for b in rows if not b[5]]
    assert returned[0][9] == 1 and returned[0][5] == "16/10/2026" and returned[0][6] == "Hỏng"
    assert active[0][9] == 2 and "[tách từ dòng" in active[0][7]
    tb002 = next(d for d in devices(env) if d[0] == "TB002")
    assert (
        tb002[2] == "Tốt" and "1 chiếc hỏng khi trả (Phạm Thị Bình) — gãy chân" in tb002[5]
    )  # 10 chiếc: không khoá cả lô
    assert "Phạm Thị Bình - 2" in tb002[6]
    assert tb002[7] == 7  # Còn lại = 10 − (Bình còn 2 + Trung Đào 1)
    # bấm xác nhận lần nữa: không ghi lại
    again = await confirm_action(env, res.for_llm["action_id"], binh)
    assert again.ok and "trước đó" in again.message
    assert len([b for b in borrows(env) if b[10] == "P-20261002-01"]) == 2

    # gia hạn phần còn lại
    res = await REGISTRY.execute(
        "extend_borrow",
        {"ma_phieu": "P-20261002-01", "han_tra_moi": "30/10", "thiet_bi": None},
        binh,
        env.svc,
    )
    assert not res.is_error, res.for_llm
    r = await confirm_action(env, res.for_llm["action_id"], binh)
    assert r.status == "DONE" and "30/10/2026" in r.message
    active = [b for b in borrows(env) if b[10] == "P-20261002-01" and not b[5]]
    assert active[0][4] == "30/10/2026" and "gia hạn 12/10/2026 → 30/10/2026" in active[0][7]
    assert any(x[3] == "equipment_extend" for x in env.lab.audit_rows())


async def test_return_validation_and_ownership(env: Env) -> None:
    an = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    with pytest.raises(ValidationFailed, match="không thể trả 5"):
        await wf_eq.propose_return(
            env.svc,
            an,
            [{"ma_phieu": "P-20261001-01", "thiet_bi": "máy chiếu", "so_luong": 5, "tinh_trang": "Tốt"}],
        )
    with pytest.raises(ValidationFailed, match="Không thấy"):  # Arduino của Bình, không phải của An
        await wf_eq.propose_return(
            env.svc, an, [{"ma_phieu": None, "thiet_bi": "arduino", "so_luong": None, "tinh_trang": "Tốt"}]
        )
    with pytest.raises(ValidationFailed, match="phải sau hạn hiện tại"):
        await wf_eq.propose_extend(env.svc, an, "P-20261001-01", "16/10/2026", None)
    # trả cả phiếu (không nêu thiết bị) → đủ số lượng, tình trạng Mất với thiết bị 2 chiếc → chỉ ghi chú
    data, _ = await wf_eq.propose_return(
        env.svc, an, [{"ma_phieu": "P-20261001-01", "thiet_bi": None, "so_luong": None, "tinh_trang": "Mất"}]
    )
    r = await confirm_action(env, data["action_id"], an)
    assert r.ok
    tb001 = next(d for d in devices(env) if d[0] == "TB001")
    assert tb001[2] == "Tốt" and "mất" in tb001[5]
    # người khác không xác nhận được yêu cầu của An
    data, _ = await wf_eq.propose_extend(
        env.svc, await ctx_for(env, "Phạm Thị Bình", Role.MEMBER, "binh"), "P-20261002-01", "25/10", None
    )
    with pytest.raises(actions.ActionInvalid):
        await actions.confirm(env.svc, data["action_id"], an)


async def test_return_rejected_when_sheet_changed(env: Env) -> None:
    binh = await ctx_for(env, "Phạm Thị Bình", Role.MEMBER, "binh")
    data, _ = await wf_eq.propose_return(
        env.svc,
        binh,
        [{"ma_phieu": "P-20261002-01", "thiet_bi": None, "so_luong": None, "tinh_trang": "Tốt"}],
    )
    # leader sửa tay trên sheet trước khi Bình bấm xác nhận
    grid = env.lab.sheets.values(env.lab.equipment_id, "LichSuMuon")
    row = next(r for r in grid if len(r) > 10 and r[10] == "P-20261002-01")
    row[5] = "15/10/2026"
    r = await confirm_action(env, data["action_id"], binh)
    assert r.status == "REJECTED" and "đã thay đổi" in r.message


# ---------------------------------------------------------------- thay đổi thành viên


def admin_ctx(sid: str = "admin") -> UserContext:
    return UserContext(Role.ADMIN, sid.ljust(32, "0"), email="admin@gmail.com", name="Thầy Admin")


async def test_leader_only_views_applications(env: Env) -> None:
    """C8: leader xem được đơn chờ duyệt nhưng không duyệt / từ chối / cho rời lab."""
    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    res = await REGISTRY.execute("list_pending_applications", {}, leader, env.svc)
    assert not res.is_error and res.for_llm["count"] >= 1
    res = await REGISTRY.execute(
        "request_member_change",
        {"action": "approve_application", "submission_id": "S1", "ho_ten": None, "role": None, "ly_do": None},
        leader,
        env.svc,
    )
    assert res.is_error
    for fn in (
        lambda: wf_mem.propose_approve(env.svc, leader, "S1", None),
        lambda: wf_mem.propose_reject(env.svc, leader, "S1", None),
        lambda: wf_mem.propose_remove(env.svc, leader, "tran thi thu"),
    ):
        with pytest.raises(PermissionDenied):
            await fn()
    # yêu cầu do admin tạo cũng không thể được leader xác nhận (kiểm tra role lúc Xác nhận)
    admin = admin_ctx()
    data, _ = await wf_mem.propose_reject(env.svc, admin, "S1", None)
    demoted = UserContext(Role.LEADER, admin.session_id, email="admin@gmail.com")
    with pytest.raises(PermissionDenied):
        await actions.confirm(env.svc, data["action_id"], demoted)


async def test_approve_application_creates_account_and_mails(env: Env) -> None:
    from app.auth import accounts

    admin = admin_ctx()
    res = await REGISTRY.execute(
        "request_member_change",
        {"action": "approve_application", "submission_id": "S1", "ho_ten": None, "role": None, "ly_do": None},
        admin,
        env.svc,
    )
    assert not res.is_error and "thêm Hoàng Văn E" in res.for_llm["summary"][0]
    r = await confirm_action(env, res.for_llm["action_id"], admin)
    assert r.status == "DONE", r
    assert "gửi mật khẩu tạm" in r.message
    snap = await env.svc.members.snapshot(force=True)
    m = snap.by_name("Hoàng Văn E")
    assert m is not None and m.role == "member" and m.email == "e@gmail.com"
    grid = env.lab.sheets.values(env.lab.members_id, "Members")
    assert grid[-1][0] == 5  # No = số tiếp theo
    sub = env.lab.sheets.values(env.lab.forms_id, "don_xin_gia_nhap")[1]
    assert sub[7] == "DA_DUYET" and "Duyệt ngày 16/10/2026" in sub[8]
    # C2: tài khoản được tạo tự động + mail mật khẩu tạm
    async with env.svc.sm() as s:
        acc = await accounts.get_by_email(s, "e@gmail.com")
    assert acc is not None and acc.must_change_password
    subjects = env.gmail.subjects()
    assert any("Tài khoản Lab Agent" in s for s in subjects)
    assert any("Thành viên mới" in s for s in subjects) and any("Chào mừng" in s for s in subjects)
    assert any("đã tự tạo tài khoản" in b for b in env.gmail.bodies())
    assert any(row[3] == "member_approve" and row[2] == "admin" for row in env.lab.audit_rows())
    with pytest.raises(ValidationFailed, match="trạng thái DA_DUYET"):
        await wf_mem.propose_approve(env.svc, admin, "S1", None)


async def test_reject_and_permissions(env: Env) -> None:
    admin = admin_ctx()
    leader = await ctx_for(env, "Nguyễn Văn Leader", Role.LEADER, "leader")
    member = await ctx_for(env, "Lê Văn An", Role.MEMBER, "an")
    res = await REGISTRY.execute(
        "request_member_change",
        {"action": "reject_application", "submission_id": "S1", "ho_ten": None, "role": None, "ly_do": None},
        member,
        env.svc,
    )
    assert res.is_error  # member không có tool này
    data, _ = await wf_mem.propose_reject(env.svc, admin, "S1", "Đủ người cho học kỳ này")
    r = await confirm_action(env, data["action_id"], admin)
    assert r.status == "DONE"
    sub = env.lab.sheets.values(env.lab.forms_id, "don_xin_gia_nhap")[1]
    assert sub[7] == "TU_CHOI" and "Từ chối ngày 16/10/2026" in sub[8]
    assert any("Đủ người" in b for b in env.gmail.bodies())
    with pytest.raises(PermissionDenied):
        await wf_mem.propose_set_role(env.svc, leader, "Lê Văn An", "thu_ki")


async def test_remove_member_blocked_by_borrow_then_ok(env: Env) -> None:
    admin = admin_ctx()
    with pytest.raises(ValidationFailed, match="còn thiết bị đang mượn"):
        await wf_mem.propose_remove(env.svc, admin, "Phạm Thị Bình")
    with pytest.raises(ValidationFailed, match="leader"):
        await wf_mem.propose_remove(env.svc, admin, "Nguyễn Văn Leader")
    data, _ = await wf_mem.propose_remove(env.svc, admin, "tran thi thu")
    r = await confirm_action(env, data["action_id"], admin)
    assert r.status == "DONE"
    snap = await env.svc.members.snapshot(force=True)
    assert snap.by_name("Trần Thị Thư") is None and snap.former_by_name("Trần Thị Thư") is not None
    assert any("đã rời lab" in s for s in env.gmail.subjects())


async def test_admin_changes_leader(env: Env) -> None:
    admin = UserContext(Role.ADMIN, "admin".ljust(32, "0"), email="admin@gmail.com")
    data, _ = await wf_mem.propose_set_role(env.svc, admin, "Lê Văn An", "leader")
    assert data["summary"] == ["Lê Văn An: Thành viên → Leader", "Nguyễn Văn Leader: Leader → Thành viên"]
    r = await confirm_action(env, data["action_id"], admin)
    assert r.status == "DONE"
    snap = await env.svc.members.snapshot(force=True)
    assert snap.leader().ho_ten == "Lê Văn An"  # type: ignore[union-attr]
    assert [m.ho_ten for m in snap.active if m.role == "leader"] == ["Lê Văn An"]  # luôn đúng 1 leader
    with pytest.raises(ValidationFailed, match="đúng 1 leader"):
        await wf_mem.propose_set_role(env.svc, admin, "Lê Văn An", "member")


# ---------------------------------------------------------------- retention


async def test_retention_archives_old_rejected_application(env: Env) -> None:
    folder = env.lab.drive.add(
        "submissions/applications/CuVanBiTuChoi_0900000000/2026-03-01_DonXinGiaNhap.pdf", "application/pdf"
    )
    folder_id = folder.parents[0]
    grid = env.lab.sheets.values(env.lab.forms_id, "don_xin_gia_nhap")
    grid.append(
        [
            "S9",
            "01/03/2026 10:00",
            "guest",
            "Cù Văn Bị Từ Chối",
            "0900000000",
            "c@gmail.com",
            f"https://drive.google.com/drive/folders/{folder_id}",
            "TU_CHOI",
            "Từ chối ngày 02/03/2026",
        ]
    )
    grid.append(
        [
            "S10",
            "01/09/2026 10:00",
            "guest",
            "Mới Bị Từ Chối",
            "0900000001",
            "m@gmail.com",
            "",
            "TU_CHOI",
            "Từ chối ngày 02/09/2026",
        ]
    )
    dry = await run_job(env.svc, "retention", dry_run=True)
    assert dry["due"] == ["S9"] and env.lab.drive.files[folder_id].parents != []
    out = await run_job(env.svc, "retention")
    assert out["moved"] == ["S9"]
    archive = env.lab.drive.files[env.lab.drive.files[folder_id].parents[0]]
    assert archive.name == "applications" and env.lab.drive.files[archive.parents[0]].name == "_archive"
    assert "[đã lưu trữ 16/10/2026]" in env.lab.sheets.values(env.lab.forms_id, "don_xin_gia_nhap")[2][8]
    assert (await run_job(env.svc, "retention"))["due"] == []  # chạy lại không chuyển lần nữa


# ---------------------------------------------------------------- API


@pytest.fixture
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import create_app

    env.svc.config.test.auth_bypass = True
    env.svc.config.test_mode = True
    app = create_app(services=env.svc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def h(c: httpx.AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies.get("la_csrf") or ""}


async def test_api_preview_confirm_and_blank(env: Env, client: httpx.AsyncClient) -> None:
    await setup_forms(env)
    me = (await client.get("/auth/me")).json()
    assert me["role"] == "member"

    # dựng ngữ cảnh giống request của client (auth_bypass: thành viên đầu tiên có role member)
    snap = await env.svc.members.snapshot()
    member = next(m for m in snap.active if m.role == "member")
    async with env.svc.sm() as s:
        from sqlalchemy import select

        from app.db.models import Session as Sess

        sess = (await s.scalars(select(Sess))).all()[-1]
    ctx = UserContext(Role.MEMBER, sess.public_id, email=member.email, member=member)
    eng, did, act = await borrow_draft(env, ctx, [{"ten_tb": "TB002", "so_luong": "1"}])
    r = await client.get(f"/drafts/{did}/preview?v=1")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["cache-control"] == "no-store"
    # phiên khác không xem được
    async with httpx.AsyncClient(transport=client._transport, base_url="http://testserver") as other:  # type: ignore[attr-defined]
        await other.get("/auth/me")
        assert (await other.get(f"/drafts/{did}/preview")).status_code == 404
        r = await other.post(
            f"/drafts/{did}/confirm", json={"shown_hash": act["shown_hash"]}, headers=h(other)
        )
        assert r.status_code == 410
    r = await client.post(f"/drafts/{did}/confirm", json={"shown_hash": "0" * 64}, headers=h(client))
    assert r.status_code == 409
    assert (
        await client.post(f"/drafts/{did}/confirm", json={"shown_hash": act["shown_hash"]})
    ).status_code == 403  # CSRF
    env.svc.config.drafts.confirm_wait_s = 0.2  # không có worker trong test này: trả "đang xử lý" ngay
    r = await client.post(f"/drafts/{did}/confirm", json={"shown_hash": act["shown_hash"]}, headers=h(client))
    body = r.json()
    assert (
        r.status_code == 200
        and body["status"] == "PROCESSING"
        and body["result"]["ma_phieu"].startswith("P-")
    )
    await OutboxWorker(env.svc).run_once()
    st = (await client.get(f"/drafts/{did}/status")).json()
    assert st["status"] == "SUBMITTED" and "Mã phiếu" in st["message"]
    # link tải đơn trống: chữ ký đúng mới tải được
    info = await eng.blank_info("phieu_muon_thiet_bi", ctx)
    url = info.ui_actions[0]["url"]
    r = await client.get(url)
    assert (
        r.status_code == 200
        and r.content.startswith(b"PK")
        and "PhieuMuonThietBi.docx" in r.headers["content-disposition"]
    )
    assert (await client.get(url.replace("sig=", "sig=x"))).status_code == 403
