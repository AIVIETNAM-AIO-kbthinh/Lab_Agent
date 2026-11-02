"""P4 end-to-end (DB thật, Drive/LLM giả): admin gửi đơn đã điền mẫu → tẩy trắng + sinh schema → sửa qua chat →
bấm Kích hoạt → mẫu trắng + _system/ trên Drive, form active → member điền hộ, xem trước, xác nhận, lưu đơn.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from docx import Document
from sqlalchemy import select

from app.auth import accounts
from app.auth.roles import Role, UserContext
from app.db.models import Session as DbSession
from app.db.models import TemplateDraft
from app.errors import DraftChanged
from app.forms.engine import FormEngine
from app.forms.placeholder import iter_paragraphs, ptext
from app.forms.schema_gen.review import TemplateReview
from app.main import create_app
from tests.conftest import Env
from tests.fakes.llm import reply
from tests.integration.test_forms_flow import (
    ctx_for,
    fv,
    make_upload,
    preview_action,
    run_confirms,
    setup_forms,
)
from tests.schema_gen_forms import NAME, de_nghi_thanh_toan, don_nghi_truc, filled_analysis

pytestmark = pytest.mark.db
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "forms"
ADMIN = UserContext(role=Role.ADMIN, session_id="admin".ljust(32, "0"), email="admin@gmail.com")


def text(docx: bytes) -> str:
    return "\n".join(ptext(p) for p in iter_paragraphs(Document(io.BytesIO(docx))))


async def propose(env: Env, data: bytes, filename: str, analysis: dict[str, Any] | None, **args: Any) -> Any:
    up = await make_upload(env, ADMIN, data, filename)
    env.anthropic.steps = [reply(tool_calls=[("submit_result", analysis)])] if analysis else []
    res = await env.svc.registry.execute("propose_template", {"upload_id": up, **args}, ADMIN, env.svc)
    assert not res.is_error, res.for_llm
    return res


async def children(env: Env, path: str) -> dict[str, Any]:
    folder = await env.svc.paths.resolve(path)
    return {c.name: c for c in await env.svc.google.drive.list_children(folder.id)}


async def test_admin_adds_filled_template_then_member_submits(env: Env) -> None:
    await setup_forms(env)
    filled = don_nghi_truc(filled=True)
    res = await propose(env, filled, "DonNghiTruc_mau.docx", filled_analysis(filled))
    ui = res.ui_actions[0]
    assert ui["type"] == "template_preview" and ui["version"] == 1 and ui["wiped"] >= 9
    assert ui["blocked"] is None
    td_id = ui["template_draft_id"]
    data = res.for_llm
    assert data["form_id"] == "don_xin_nghi_truc" and data["filled_sample_wiped"] is True
    assert "untrusted_content" in data["content"]  # nhãn/ghi chú lấy từ file là dữ liệu
    prompt = str(env.anthropic.calls[0]["messages"])
    assert "<document>" in prompt and "Nguyễn Văn Mẫu" in prompt

    rv = TemplateReview(env.svc)
    td = await rv.owned(td_id, ADMIN)
    assert NAME not in text(td.blank_docx) and "0912345678" not in text(td.blank_docx)
    assert "{{ ho_ten }}" in text(td.template_docx)

    # admin sửa qua chat: bỏ ngày sinh, bắt buộc người trực thay, thêm guest? không — đổi tên đơn
    res2 = await env.svc.registry.execute(
        "update_template_draft",
        {
            "template_draft_id": td_id,
            "title": "Đơn xin nghỉ trực lab",
            "changes": [
                {"action": "remove", "key": "ngay_sinh"},
                {"action": "update", "key": "Người trực thay", "required": True, "type": "member_ref"},
            ],
        },
        ADMIN,
        env.svc,
    )
    assert not res2.is_error, res2.for_llm
    ui2 = res2.ui_actions[0]
    assert ui2["version"] == 2 and ui2["shown_hash"] != ui["shown_hash"]
    assert all(r[0] != "• Ngày sinh" for r in ui2["summary"])
    with pytest.raises(DraftChanged):  # nút của bản xem trước cũ bị từ chối
        await rv.confirm(td_id, ui["shown_hash"], ADMIN)

    [r] = await run_confirms(env, rv.confirm(td_id, ui2["shown_hash"], ADMIN))
    assert r.ok and r.status == "ACTIVATED", r
    again = await rv.confirm(td_id, ui2["shown_hash"], ADMIN)  # bấm đúp
    assert again.ok and "trước đó" in again.message

    fd = await env.svc.forms.active("don_xin_nghi_truc")
    assert fd is not None and fd.version == "2026-10-16" and fd.template_file_id
    assert fd.schema.template.drive_path == "templates/DonXinNghiTrucLab.docx" and fd.schema.template.file_id
    tpl_files = await children(env, "templates")
    assert NAME not in text(env.lab.drive.content[tpl_files["DonXinNghiTrucLab.docx"].id])
    assert set(await children(env, "templates/_system/don_xin_nghi_truc/v2026-10-16")) == {
        "template.docx",
        "schema.yaml",
    }
    assert any("template_activate" in str(row) for row in env.lab.audit_rows())

    # member dùng mẫu mới qua form engine
    member = await ctx_for(env, "Lê Văn An", Role.MEMBER, "mem1")
    listed = await env.svc.registry.execute("list_forms", {}, member, env.svc)
    assert "don_xin_nghi_truc" in {f["form_id"] for f in listed.for_llm["forms"]}
    blank = await env.svc.registry.execute(
        "get_form_template", {"form_id": "don_xin_nghi_truc"}, member, env.svc
    )
    assert blank.ui_actions[0]["type"] == "download"
    eng = FormEngine(env.svc)
    st = await eng.start("don_xin_nghi_truc", member)
    did = st.data["draft_id"]
    assert st.data["filled"]["Họ và tên"] == "Lê Văn An"  # lấy từ tài khoản
    upd = await eng.update(
        did,
        [
            fv("buoi", "chiều"),
            fv("so_buoi", "1"),
            fv("ly_do", "Đi khám bệnh"),
            fv("ngay_nghi", "20/10/2026"),
            fv("nguoi_truc_thay", "Phạm Thị Bình"),
        ],
        member,
    )
    assert upd.data["missing"] == [] and upd.data["errors"] == [], upd.data
    act = preview_action(await eng.preview(did, member))
    [cr] = await run_confirms(env, eng.confirm(did, act["shown_hash"], member))
    assert cr.ok and cr.status == "SUBMITTED", cr
    rows = env.lab.sheets.values(env.lab.forms_id, "don_xin_nghi_truc")
    header, row = rows[0], rows[1]
    rec = dict(zip(header, row, strict=False))
    assert rec["nguoi_nop"] == "Lê Văn An" and rec["buoi"] == "Chiều" and rec["trang_thai"] == "DA_NOP"
    saved = await children(env, "submissions/don_xin_nghi_truc/2026-10")
    assert [n for n in saved if n.startswith("2026-10-16_LeVanAn_DonXinNghiTrucLab")]


async def test_replace_generic_form_archives_old_blank(env: Env) -> None:
    rv = TemplateReview(env.svc)
    for i in (1, 2):
        res = await propose(env, de_nghi_thanh_toan(), f"GiayDeNghi{i}.docx", None)
        ui = res.ui_actions[0]
        if i == 2:  # đã có mẫu cùng mã: cảnh báo thay phiên bản
            assert any("thay phiên bản" in w for w in ui["warnings"])
        [r] = await run_confirms(env, rv.confirm(ui["template_draft_id"], ui["shown_hash"], ADMIN))
        assert r.ok, r
    fd = await env.svc.forms.active("giay_de_nghi_thanh_toan")
    assert fd is not None and fd.version == "2026-10-16.2"
    assert "GiayDeNghiThanhToan.docx" in await children(env, "templates/_archive")
    assert fd.schema.template.file_id == (await children(env, "templates"))["GiayDeNghiThanhToan.docx"].id
    old = await env.svc.forms.get("giay_de_nghi_thanh_toan", "2026-10-16")
    assert old is not None and old.status == "inactive"


async def test_hooked_form_id_is_blocked_until_renamed(env: Env) -> None:
    await setup_forms(env)
    res = await propose(env, (FIX / "PhieuMuonThietBi.docx").read_bytes(), "PhieuMuonThietBi.docx", None)
    ui = res.ui_actions[0]
    assert ui["blocked"] and "equipment_borrow" in ui["blocked"]  # LLM lỗi → chỉ luật, tên trùng đơn có hook
    assert any("AI" in w for w in ui["warnings"])
    rv = TemplateReview(env.svc)
    r = await rv.confirm(ui["template_draft_id"], ui["shown_hash"], ADMIN)
    assert not r.ok and r.status == "NEEDS_EDIT"
    res2 = await env.svc.registry.execute(
        "update_template_draft",
        {"template_draft_id": ui["template_draft_id"], "form_id": "phieu_muon_phong_hop"},
        ADMIN,
        env.svc,
    )
    assert not res2.is_error and res2.ui_actions[0]["blocked"] is None


async def test_permissions_and_bad_inputs(env: Env) -> None:
    member = UserContext(role=Role.MEMBER, session_id="m".ljust(32, "0"))
    assert "propose_template" not in {t.name for t in env.svc.registry.for_role(Role.MEMBER)}
    assert "propose_template" in {t.name for t in env.svc.registry.for_role(Role.ADMIN)}
    denied = await env.svc.registry.execute("propose_template", {"upload_id": "x"}, member, env.svc)
    assert denied.is_error
    pdf = await make_upload(env, ADMIN, b"%PDF-1.4 x", "don.pdf")
    bad = await env.svc.registry.execute("propose_template", {"upload_id": pdf}, ADMIN, env.svc)
    assert bad.is_error and ".docx" in bad.for_llm["error"]
    res = await propose(env, de_nghi_thanh_toan(), "x.docx", None)
    td_id = res.ui_actions[0]["template_draft_id"]
    other_admin = UserContext(role=Role.ADMIN, session_id="other".ljust(32, "0"))
    stolen = await env.svc.registry.execute(
        "cancel_template_draft", {"template_draft_id": td_id}, other_admin, env.svc
    )
    assert stolen.is_error  # bản duyệt thuộc session khác
    bad_add = await env.svc.registry.execute(
        "update_template_draft",
        {
            "template_draft_id": td_id,
            "changes": [{"action": "add", "key": "x", "label": "X", "anchor": "không có trong đơn"}],
        },
        ADMIN,
        env.svc,
    )
    assert not bad_add.is_error and bad_add.for_llm["errors"]
    ok = await env.svc.registry.execute("cancel_template_draft", {"template_draft_id": td_id}, ADMIN, env.svc)
    assert ok.for_llm["status"] == "CANCELLED"


# ---------------------------------------------------------------- endpoint nút bấm


@pytest.fixture
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(services=env.svc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def test_template_endpoints(client: httpx.AsyncClient, env: Env) -> None:
    async with env.svc.sm() as s, s.begin():
        await accounts.create_account(s, "admin@gmail.com", "Mat-Khau-Rat-Dai-2026")
        acc = await accounts.get_by_email(s, "admin@gmail.com")
        assert acc is not None
        acc.must_change_password = False
    await client.get("/auth/me")
    csrf = {"X-CSRF-Token": client.cookies.get("la_csrf") or ""}
    await client.post(
        "/auth/login", json={"email": "admin@gmail.com", "password": "Mat-Khau-Rat-Dai-2026"}, headers=csrf
    )
    csrf = {"X-CSRF-Token": client.cookies.get("la_csrf") or ""}
    async with env.svc.sm() as s:
        sess = (await s.scalars(select(DbSession).where(DbSession.account_id == acc.id))).first()
    assert sess is not None
    admin = UserContext(role=Role.ADMIN, session_id=sess.public_id, email="admin@gmail.com")
    up = await make_upload(env, admin, de_nghi_thanh_toan(), "GiayDeNghi.docx")
    res = await TemplateReview(env.svc).propose(up, admin)
    ui = res.ui_actions[0]
    r = await client.get(ui["url"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    r = await client.get(ui["blank_url"])
    assert r.status_code == 200 and "GiayDeNghiThanhToan.docx" in r.headers["content-disposition"]
    r = await client.post(
        f"/admin/api/templates/{ui['template_draft_id']}/confirm", json={"shown_hash": "0" * 64}
    )
    assert r.status_code == 403  # thiếu CSRF
    r = await client.post(
        f"/admin/api/templates/{ui['template_draft_id']}/confirm", json={"shown_hash": "0" * 64}, headers=csrf
    )
    assert r.status_code == 409
    listing = (await client.get("/admin/api/templates")).json()
    assert listing["pending"][0]["id"] == ui["template_draft_id"]
    r = await client.post(f"/admin/api/templates/{ui['template_draft_id']}/cancel", headers=csrf)
    assert r.json()["status"] == "CANCELLED"
    async with env.svc.sm() as s:
        assert (await s.get(TemplateDraft, ui["template_draft_id"])).status == "CANCELLED"  # type: ignore[union-attr]
    await client.post("/auth/logout", headers=csrf)
    assert (await client.get(ui["url"])).status_code == 403  # guest
