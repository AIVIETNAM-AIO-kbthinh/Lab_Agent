"""P4 — tự sinh schema (không cần DB/LLM thật).

"Xong khi": 5 đơn trống mẫu cho ra đúng trường; schema sinh từ 3 đơn ban đầu tương đương `schemas/`.
- 3 đơn thật (tests/fixtures/forms) + 2 đơn tổng hợp (tests/schema_gen_forms.py).
- Tẩy trắng đơn đã điền mẫu cho ra đúng các ô như đơn trắng.
- Ô kiểu mới (ô bảng, ô tick, nhãn lặp) chèn placeholder, render và trích xuất ngược lại được.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from docx import Document

from app.forms.extract import extract_by_rules, text_of
from app.forms.placeholder import insert_placeholders, iter_paragraphs, ptext
from app.forms.publish import sample_values, self_test
from app.forms.render import build_context, render_docx
from app.forms.schema import FormSchema, load_schema_dir
from app.forms.schema_gen.blanks import detect
from app.forms.schema_gen.build import build_schema
from app.forms.schema_gen.compare import compare
from app.forms.schema_gen.llm_sweep import FormAnalysis, build_prompt
from app.forms.schema_gen.pipeline import GenerationFailed, generate
from app.forms.schema_gen.whiteout import whiteout
from tests.schema_gen_forms import de_nghi_thanh_toan, don_nghi_truc, filled_analysis, para_id

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "forms"
SCHEMAS = load_schema_dir(ROOT / "schemas")
REAL = {
    "don_xin_gia_nhap": "DonXinGiaNhap.docx",
    "phieu_muon_thiet_bi": "PhieuMuonThietBi.docx",
    "bien_ban_hop": "BienBanHop.docx",
}
TODAY = date(2026, 10, 26)


async def gen(data: bytes, name: str, **kw: Any) -> Any:
    return await generate(data, name, llm=None, today=TODAY, **kw)


def text(docx: bytes) -> str:
    return "\n".join(ptext(p) for p in iter_paragraphs(Document(io.BytesIO(docx))))


# ---------------------------------------------------------------- ① luật trên 3 đơn thật


@pytest.mark.parametrize("form_id", list(REAL))
def test_rules_find_every_handwritten_anchor(form_id: str) -> None:
    det = detect((FIX / REAL[form_id]).read_bytes())
    found = {c.anchor for c in det.fields()}
    want = {f.anchor for f in SCHEMAS[form_id].fields if f.anchor}
    assert want <= found, want - found
    assert {s.label for s in det.signatures()} == {s.label for s in SCHEMAS[form_id].signatures}


@pytest.mark.parametrize("form_id", ["don_xin_gia_nhap", "phieu_muon_thiet_bi"])
async def test_rules_only_schema_equivalent_to_handwritten(form_id: str) -> None:
    ref = SCHEMAS[form_id]
    res = await gen((FIX / REAL[form_id]).read_bytes(), REAL[form_id], roles=list(ref.roles))
    diff = compare(res.schema, ref)
    assert diff.equivalent, diff.as_dict()
    assert self_test(res.template, res.schema) == []
    assert res.report.leftovers == []


def _bien_ban_analysis(det: Any) -> FormAnalysis:
    """LLM (kịch bản) chỉ bổ sung phần ngữ nghĩa mà luật không biết: lựa chọn 'Hình thức', ngày họp do người dùng."""
    fields = []
    for c in det.fields():
        f: dict[str, Any] = {
            "candidate": c.cid,
            "anchor": None,
            "blank": None,
            "key": "",
            "label": c.label,
            "type": "text",
            "required": True,
            "filled_by": "user",
            "options": [],
            "multiple": False,
            "hint": None,
            "columns": [],
        }
        if c.kind == "date_parts":
            f.update(key="ngay_hop", label="Ngày họp", type="date")
        elif c.label == "Hình thức":
            f.update(type="choice", options=["Trực tiếp", "Trực tuyến", "Kết hợp"])
        elif c.label in ("Chủ trì", "Thư ký"):
            f.update(type="member_ref")
        elif c.label in ("Thành viên tham dự", "Vắng mặt"):
            f.update(type="member_ref", multiple=True)
        elif c.kind == "next_line":
            f.update(type="longtext")
        elif c.kind == "table":
            f.update(type="table")
        elif "thời gian bắt đầu" in c.label.lower() or "kết thúc" in c.label.lower():
            f.update(type="time")
        fields.append(f)
    return FormAnalysis.model_validate(
        {
            "title": "Biên bản họp",
            "description": "Biên bản cuộc họp lab.",
            "is_filled_sample": False,
            "sample_values": [],
            "fields": fields,
            "ignore": [],
            "signatures": [
                {"label": "THƯ KÝ", "signer_key": "thu_ky"},
                {"label": "CHỦ TRÌ", "signer_key": "chu_tri"},
            ],
            "suggested_roles": ["thu_ki", "leader"],
            "notes": [],
        }
    )


def test_bien_ban_with_llm_semantics_equivalent() -> None:
    ref = SCHEMAS["bien_ban_hop"]
    det = detect((FIX / "BienBanHop.docx").read_bytes())
    schema, notes = build_schema(det, _bien_ban_analysis(det), today=TODAY)
    diff = compare(schema, ref)
    assert diff.equivalent, diff.as_dict()
    assert schema.roles == ["thu_ki", "leader"]
    hinh_thuc = next(f for f in schema.fields if f.label == "Hình thức")
    assert hinh_thuc.options == ["Trực tiếp", "Trực tuyến", "Kết hợp"]
    ngay = schema.field("ngay_hop")
    assert ngay is not None and ngay.filled_by == "user" and ngay.default == "today"
    assert notes.warnings == []


async def test_bien_ban_rules_only_differs_only_in_semantics() -> None:
    res = await gen((FIX / "BienBanHop.docx").read_bytes(), "BienBanHop.docx", roles=["thu_ki", "leader"])
    diff = compare(res.schema, SCHEMAS["bien_ban_hop"])
    assert not diff.missing and not diff.extra
    assert {m.split(":")[0] for m in diff.mismatches} == {"ngay_hop", "hinh_thuc"}


# ---------------------------------------------------------------- 2 đơn tổng hợp


async def test_synthetic_leave_form_fields() -> None:
    res = await gen(don_nghi_truc(), "DonXinNghiTruc.docx")
    s: FormSchema = res.schema
    got = {f.key: (f.type, f.blank, f.filled_by) for f in s.fields}
    assert got == {
        "ngay_lap": ("date", "date_parts", "system"),
        "ho_ten": ("text", "ellipsis", "user"),
        "ngay_sinh": ("date", "ellipsis", "user"),
        "email": ("text", "inline", "user"),
        "sdt": ("text", "inline", "user"),
        "buoi_xin_nghi": ("choice", "checkbox", "user"),
        "so_buoi_nghi": ("int", "inline", "user"),
        "ly_do": ("longtext", "next_line", "user"),
        "ngay_xin_nghi": ("date", "cell", "user"),
        "nguoi_truc_thay": ("text", "cell", "user"),
        "dong_y_khong_dong_y": ("text", "inline", "lab"),
    }
    # đơn cho thành viên: họ tên/email/SĐT lấy từ tài khoản
    assert s.field("ho_ten").locked and s.field("ho_ten").label == "Họ và tên"  # type: ignore[union-attr]
    assert s.field("buoi_xin_nghi").options == ["Sáng", "Chiều"]  # type: ignore[union-attr]
    assert [(x.label, x.field) for x in s.signatures] == [
        ("XÁC NHẬN CỦA LEADER", None),
        ("NGƯỜI LÀM ĐƠN", "ho_ten"),
    ]
    assert s.form_id == "don_xin_nghi_truc_lab" and s.hook is None
    assert s.template.drive_path == "templates/DonXinNghiTrucLab.docx"
    assert s.output.folder == "submissions/don_xin_nghi_truc_lab/{today:%Y-%m}"
    assert res.report.leftovers == [] and res.report.dropped == []


async def test_synthetic_payment_form_fields() -> None:
    res = await gen(de_nghi_thanh_toan(), "GiayDeNghiThanhToan.docx", roles=["guest", "member"])
    s = res.schema
    labels = [f.label for f in s.fields]
    assert labels[:4] == [
        "Họ và tên (người đề nghị)",
        "Số điện thoại (người đề nghị)",
        "Họ và tên (người nhận tiền)",
        "Số điện thoại (người nhận tiền)",
    ]
    assert not any(f.locked for f in s.fields)  # có guest ⇒ không khoá theo tài khoản
    t = s.field("danh_sach_khoan_chi")
    assert t is not None and [c.type for c in t.columns] == ["row_number", "text", "text", "text"]
    att = s.field("chung_tu_dinh_kem")
    assert att is not None and att.type == "attachment"
    assert s.field("hinh_thuc_nhan_tien").options == ["Tiền mặt", "Chuyển khoản"]  # type: ignore[union-attr]


async def test_new_blank_kinds_render_and_extract_round_trip() -> None:
    """Ô tick, ô bảng, nhãn lặp: điền → render → trích xuất lại đúng giá trị."""
    res = await gen(de_nghi_thanh_toan(), "GiayDeNghiThanhToan.docx", roles=["guest"])
    s = res.schema
    fields = {
        "ho_ten": "Nguyễn A",
        "sdt": "0911111111",
        "ho_va_ten": "Trần B",
        "so_dien_thoai": "0922222222",
        "noi_dung_thanh_toan": "Mua linh kiện",
        "hinh_thuc_nhan_tien": "Chuyển khoản",
        "danh_sach_khoan_chi": [{"stt": 1, "noi_dung_chi": "Arduino", "so_tien": "200000", "ghi_chu": ""}],
    }
    out = render_docx(res.template, build_context(s, fields, {}, {}))
    body = text(out)
    assert "☐ Tiền mặt" in body and "☒ Chuyển khoản" in body
    assert "{{" not in body and "Arduino" in body
    lines, doc = text_of(out, "docx")
    raw = extract_by_rules(s, lines, doc)
    assert raw["ho_ten"] == "Nguyễn A" and raw["ho_va_ten"] == "Trần B"  # nhãn lặp: lần xuất hiện thứ 2
    assert raw["sdt"] == "0911111111" and raw["so_dien_thoai"] == "0922222222"
    assert raw["hinh_thuc_nhan_tien"] == "Chuyển khoản"

    leave = (await gen(don_nghi_truc(), "x.docx", roles=["guest"])).schema
    tpl, rep = insert_placeholders(don_nghi_truc(), leave)
    assert rep.ok, rep.missing
    vals = {"ngay_xin_nghi": "25/10/2026", "nguoi_truc_thay": "Trần Thị B", "buoi_xin_nghi": "Sáng"}
    filled = render_docx(tpl, build_context(leave, vals, {}, {}))
    lines, doc = text_of(filled, "docx")
    raw = extract_by_rules(leave, lines, doc)
    assert raw["ngay_xin_nghi"] == "25/10/2026" and raw["nguoi_truc_thay"] == "Trần Thị B"
    assert raw["buoi_xin_nghi"] == "Sáng"


@pytest.mark.parametrize("make", [don_nghi_truc, de_nghi_thanh_toan])
async def test_synthetic_self_test_passes(make: Any) -> None:
    res = await gen(make(), "x.docx")
    assert self_test(res.template, res.schema) == []
    f, a = sample_values(res.schema)
    assert "{{" not in text(render_docx(res.template, build_context(res.schema, f, a, {})))


# ---------------------------------------------------------------- tẩy trắng đơn đã điền


def test_whiteout_filled_form_matches_blank_form() -> None:
    filled = don_nghi_truc(filled=True)
    an = FormAnalysis.model_validate(filled_analysis(filled))
    blank, rep = whiteout(filled, an.sample_values)
    assert rep.not_found == []
    body = text(blank)
    for secret in (
        "Nguyễn Văn Mẫu",
        "mau@gmail.com",
        "0912345678",
        "01/02/2004",
        "Về quê",
        "20 tháng 10",
        "☒",
    ):
        assert secret not in body
    assert "Kính gửi: Leader phòng thí nghiệm" in body  # chữ in sẵn giữ nguyên
    got = [(c.kind, c.anchor) for c in detect(blank).candidates]
    want = [(c.kind, c.anchor) for c in detect(don_nghi_truc()).candidates]
    assert got == want


def test_ticked_box_in_filled_form_detected_with_clean_anchor() -> None:
    det = detect(don_nghi_truc(filled=True))
    box = next(c for c in det.fields() if c.kind == "checkbox")
    assert box.anchor == "Buổi xin nghỉ:" and box.options == ["Sáng", "Chiều"]


def test_whiteout_rejects_spans_not_in_paragraph() -> None:
    filled = don_nghi_truc(filled=True)
    an = FormAnalysis.model_validate(
        filled_analysis(filled)
        | {"sample_values": [{"para": para_id(filled, "Kính gửi"), "text": "không có"}]}
    )
    blank, rep = whiteout(filled, an.sample_values, filled=False)
    assert rep.not_found and "Kính gửi: Leader phòng thí nghiệm" in text(blank)


def test_build_with_llm_on_filled_form() -> None:
    filled = don_nghi_truc(filled=True)
    an = FormAnalysis.model_validate(filled_analysis(filled))
    blank, _ = whiteout(filled, an.sample_values)
    schema, notes = build_schema(detect(blank), an, orig=detect(filled), today=TODAY)
    assert [f.key for f in schema.fields] == [
        "ngay_lap",
        "ho_ten",
        "ngay_sinh",
        "email",
        "sdt",
        "buoi",
        "so_buoi",
        "ly_do",
        "ngay_nghi",
        "nguoi_truc_thay",
        "y_kien",
    ]
    assert schema.title == "Đơn xin nghỉ trực" and schema.form_id == "don_xin_nghi_truc"
    assert schema.field("ly_do").label == "Lý do xin nghỉ"  # type: ignore[union-attr]
    assert schema.field("y_kien").filled_by == "lab"  # type: ignore[union-attr]
    assert notes.warnings == [] and notes.llm_notes


def test_llm_prompt_wraps_document_as_data() -> None:
    det = detect(don_nghi_truc())
    p = build_prompt(det, "x.docx", None)
    assert "<document>" in p and "[p5] Tôi tên là" in p and "c2 (ellipsis" in p


async def test_no_blank_without_llm_fails_clearly() -> None:
    doc = Document()
    doc.add_paragraph("Họ và tên: Nguyễn Văn A")
    buf = io.BytesIO()
    doc.save(buf)
    with pytest.raises(GenerationFailed):
        await gen(buf.getvalue(), "x.docx")
    with pytest.raises(GenerationFailed):
        await gen(b"not a docx", "x.docx")
