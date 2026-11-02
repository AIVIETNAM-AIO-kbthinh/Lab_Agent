"""Form engine — phần không cần DB: schema, chèn placeholder trên 3 mẫu đơn thật, validators, trích xuất."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.domain.equipment import Borrow, Device, EquipmentSnapshot
from app.domain.members import Member, MembersSnapshot
from app.forms.extract import extract_by_rules, text_of
from app.forms.placeholder import insert_placeholders
from app.forms.publish import sample_values, self_test
from app.forms.render import build_context, render_docx, summary_lines
from app.forms.schema import FormSchema, load_schema_dir
from app.forms.validators import (
    VContext,
    check_draft,
    normalize_table,
    normalize_value,
    parse_time_input,
)

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "forms"
TPL = {
    "don_xin_gia_nhap": "DonXinGiaNhap.docx",
    "phieu_muon_thiet_bi": "PhieuMuonThietBi.docx",
    "bien_ban_hop": "BienBanHop.docx",
}
TODAY = date(2026, 10, 16)


@pytest.fixture(scope="module")
def schemas() -> dict[str, FormSchema]:
    return load_schema_dir(ROOT / "schemas")


def vc(**kw: object) -> VContext:
    members = MembersSnapshot(
        [
            Member(4, "Nguyễn Văn Leader", "leader@gmail.com", "leader"),
            Member(5, "Trần Thị Thư", "thu@gmail.com", "thu_ki"),
            Member(6, "Lê Văn An", "an@gmail.com", "member"),
            Member(7, "Lê Văn Anh", "anh@gmail.com", "member"),
            Member(8, "Phạm Thị Bình", "binh@vnu.edu.vn", "member"),
        ]
    )
    devices = [
        Device(2, "TB001", "Máy chiếu Epson", "Tốt", 2),
        Device(3, "TB002", "Arduino Uno", "Tốt", 10),
        Device(4, "TB003", "Máy in 3D", "Hỏng", 1),
    ]
    borrows = [
        Borrow(2, TODAY, "Lê Văn An", "Máy chiếu Epson", "Tốt", TODAY, None, "", "", "TB001", 1, "P-1", ""),
    ]
    base = {
        "today": TODAY,
        "allowed_domains": ["gmail.com", "vnu.edu.vn"],
        "members": members,
        "equipment": EquipmentSnapshot(devices, borrows, ["Hỏng", "Mất"]),
        "advisor_name": "Thầy Nguyễn Đăng Khoa",
    }
    base.update(kw)
    return VContext(**base)  # type: ignore[arg-type]


def test_schemas_load_and_invitation(schemas: dict[str, FormSchema]) -> None:
    assert set(schemas) == set(TPL)
    app = schemas["don_xin_gia_nhap"]
    assert app.roles == ["guest"] and app.hook == "application"
    inv = app.invitation()
    assert "họ và tên" in inv and "CV" in inv and "ngày làm đơn" not in inv  # trường hệ thống không hỏi
    phieu = schemas["phieu_muon_thiet_bi"]
    assert all(f.locked for f in phieu.fields if f.account_field)
    assert "họ và tên" not in phieu.invitation()  # lấy từ tài khoản, không hỏi


def test_schema_rejects_bad_reference() -> None:
    raw = load_schema_dir(ROOT / "schemas")["phieu_muon_thiet_bi"].dump()
    raw["fields"][-3]["after"] = "khong_ton_tai"
    with pytest.raises(ValueError, match="không tồn tại"):
        FormSchema.model_validate(raw)


@pytest.mark.parametrize("form_id", list(TPL))
def test_placeholders_on_real_templates(schemas: dict[str, FormSchema], form_id: str) -> None:
    s = schemas[form_id]
    tpl, rep = insert_placeholders((FIX / TPL[form_id]).read_bytes(), s)
    assert rep.ok, rep.missing
    expected = {f.key for f in s.fields if f.anchor and f.filled_by != "lab"}
    assert set(rep.placed) == expected
    assert rep.signatures == [x.label for x in s.signatures if x.field]
    assert self_test(tpl, s) == []


def test_render_escapes_and_marks_unknown(schemas: dict[str, FormSchema]) -> None:
    s = schemas["bien_ban_hop"]
    tpl, _ = insert_placeholders((FIX / TPL["bien_ban_hop"]).read_bytes(), s)
    fields, atts = sample_values(s, TODAY)
    fields["noi_dung"] = "A & B <c>\nDòng 2"
    fields["thanh_vien_tham_du"] = ["Lê Văn An", "Khách mời X"]
    meta = {"unknown": {"thanh_vien_tham_du": ["Khách mời X"]}}
    out = render_docx(tpl, build_context(s, fields, atts, meta))
    lines, _ = text_of(out, "docx")
    text = "\n".join(lines)
    assert "A & B <c>" in text and "Dòng 2" in text
    assert "Khách mời X (ngoài lab)" in text
    assert "{{" not in text
    rows = dict((k, v) for k, v in summary_lines(s, fields, atts, meta) if not k.startswith("Phân công"))
    assert rows["Thành viên tham dự"] == "Lê Văn An, Khách mời X (ngoài lab)"


def test_scalar_validators() -> None:
    from app.forms.schema import ColumnSpec

    v = vc()
    phone = ColumnSpec(key="p", label="SĐT", type="phone")
    assert normalize_value(phone, "+84 912 345 678", v).value == "0912345678"
    assert normalize_value(phone, "12345", v).error
    email = ColumnSpec(key="e", label="Email", type="email")
    assert normalize_value(email, "a@gmail.com", v).value == "a@gmail.com"
    assert "tên miền" in (normalize_value(email, "a@yahoo.com", v).error or "")
    d = ColumnSpec(key="d", label="Ngày", type="date")
    assert normalize_value(d, "20/10", v).value == "20/10/2026"
    assert normalize_value(d, "ngày mai", v).value == "17/10/2026"
    assert normalize_value(d, "ngày 5 tháng 11", v).value == "05/11/2026"
    assert normalize_value(d, "31/02/2026", v).error
    assert parse_time_input("14h30") == "14:30" and parse_time_input("2 giờ chiều") == "14:00"
    assert parse_time_input("25:00") is None
    choice = ColumnSpec(key="c", label="Hình thức", type="choice", options=["Trực tiếp", "Trực tuyến"])
    assert normalize_value(choice, "truc tuyen", v).value == "Trực tuyến"
    assert normalize_value(choice, "qua thư", v).options == ["Trực tiếp", "Trực tuyến"]
    n = ColumnSpec(key="n", label="SL", type="int", min=1)
    assert normalize_value(n, "0", v).error and normalize_value(n, "3", v).value == 3


def test_member_ref_resolution() -> None:
    from app.forms.schema import ColumnSpec

    v = vc()
    one = ColumnSpec(key="m", label="Người", type="member_ref")
    assert normalize_value(one, "pham thi binh", v).value == "Phạm Thị Bình"
    amb = normalize_value(one, "Lê Văn", v)
    assert amb.error and set(amb.options) >= {"Lê Văn An", "Lê Văn Anh"}
    assert "Không tìm thấy" in (normalize_value(one, "Ngô Bảo Châu", v).error or "")
    adv = ColumnSpec(key="c", label="Chủ trì", type="member_ref", allow_admin=True)
    assert normalize_value(adv, "thầy Khoa", v).value == "Thầy Nguyễn Đăng Khoa"
    many = ColumnSpec(key="t", label="Tham dự", type="member_ref", multiple=True, allow_unknown=True)
    r = normalize_value(many, "Phạm Thị Bình, Trần Thị Thư và Khách mời X", v)
    assert r.value == ["Phạm Thị Bình", "Trần Thị Thư", "Khách mời X"] and r.unknown == ["Khách mời X"]


def test_equipment_table_rules(schemas: dict[str, FormSchema]) -> None:
    s = schemas["phieu_muon_thiet_bi"]
    f = s.field("thiet_bi")
    assert f is not None
    v = vc()
    rows, issues, notes = normalize_table(f, [{"ten_tb": "arduino", "so_luong": "4"}, {"ten_tb": "TB001"}], v)
    assert not issues
    assert rows[0] == {
        "stt": 1,
        "ten_tb": "Arduino Uno",
        "ma_tb": "TB002",
        "so_luong": 4,
        "tinh_trang": "Tốt",
        "ghi_chu": None,
    }
    assert rows[1]["so_luong"] == 1 and rows[1]["ma_tb"] == "TB001"  # mặc định SL = 1
    assert any("Arduino Uno" in n for n in notes)
    _, bad, _ = normalize_table(f, [{"ten_tb": "máy in 3D", "so_luong": "1"}], v)
    assert "không cho mượn" in bad[0].message
    # cộng dồn theo ma_tb: máy chiếu còn 1 (An đang mượn 1/2)
    fields = {
        "ho_ten": "Lê Văn An",
        "vai_tro": "Thành viên",
        "muc_dich": "Demo",
        "dia_diem": "P305",
        "ngay_muon": "16/10/2026",
        "han_tra": "15/10/2026",
        "thiet_bi": [
            {"stt": 1, "ten_tb": "Máy chiếu Epson", "ma_tb": "TB001", "so_luong": 1, "tinh_trang": "Tốt"},
            {"stt": 2, "ten_tb": "Máy chiếu Epson", "ma_tb": "TB001", "so_luong": 1, "tinh_trang": "Tốt"},
        ],
    }
    msgs = [i.message for i in check_draft(s, fields, {}, v)]
    assert any("chỉ còn 1 (đang yêu cầu 2)" in m for m in msgs)
    assert any("phải sau" in m for m in msgs)  # hạn trả trước ngày mượn
    fields["ngay_muon"] = "10/10/2026"
    assert any("không được trước hôm nay" in i.message for i in check_draft(s, fields, {}, v))


def test_required_and_attachment_missing(schemas: dict[str, FormSchema]) -> None:
    s = schemas["don_xin_gia_nhap"]
    issues = check_draft(s, {"ho_ten": "A"}, {}, vc())
    labels = {i.label for i in issues}
    assert {"Mã số sinh viên", "Số điện thoại", "Email", "CV (PDF hoặc DOCX)"} <= labels
    assert "Ngày sinh" not in labels  # không bắt buộc


@pytest.mark.parametrize("form_id", list(TPL))
def test_extract_round_trip(schemas: dict[str, FormSchema], form_id: str) -> None:
    """Đơn 'user tự điền' (render từ mẫu thật) → luật theo anchor đọc lại đúng giá trị."""
    s = schemas[form_id]
    tpl, _ = insert_placeholders((FIX / TPL[form_id]).read_bytes(), s)
    fields, atts = sample_values(s, TODAY)
    filled = render_docx(tpl, build_context(s, fields, atts, {}))
    lines, doc = text_of(filled, "docx")
    raw = extract_by_rules(s, lines, doc)
    for f in s.user_fields():
        if f.type == "attachment" or not f.anchor:
            continue
        got, want = raw.get(f.key), fields[f.key]
        if f.type == "table":
            assert [r.get(f.columns[1].key) for r in got] == [str(r[f.columns[1].key]) for r in want]
        elif f.multiple:
            assert got == ", ".join(want)
        else:
            assert got == str(want), f.key


def test_blank_link_signature(loaded) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from app.forms.links import blank_url, verify_blank

    svc = SimpleNamespace(settings=loaded.settings, config=loaded.config)
    url = blank_url(svc, "don_xin_gia_nhap", now=1000)  # type: ignore[arg-type]
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    assert verify_blank(svc, "don_xin_gia_nhap", int(q["exp"]), q["sig"], now=1001)  # type: ignore[arg-type]
    assert not verify_blank(svc, "phieu_muon_thiet_bi", int(q["exp"]), q["sig"], now=1001)  # type: ignore[arg-type]
    assert not verify_blank(svc, "don_xin_gia_nhap", int(q["exp"]), q["sig"], now=int(q["exp"]) + 1)  # type: ignore[arg-type]


def test_absent_cannot_also_attend(schemas: dict[str, FormSchema]) -> None:
    s = schemas["bien_ban_hop"]
    fields, _ = sample_values(s, TODAY)
    fields.update(
        ngay_hop="16/10/2026",
        thanh_vien_tham_du=["Lê Văn An", "Phạm Thị Bình"],
        vang_mat=["Phạm Thị Bình"],
        phan_cong=[],
    )
    msgs = [i.message for i in check_draft(s, fields, {}, vc())]
    assert any("Phạm Thị Bình vừa có trong 'Thành viên tham dự'" in m for m in msgs)
