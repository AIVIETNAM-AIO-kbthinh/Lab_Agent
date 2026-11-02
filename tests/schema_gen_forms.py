"""Mẫu đơn tổng hợp cho test P4 (dựng bằng python-docx), bổ sung cho 3 đơn thật trong tests/fixtures/forms/.

- `don_nghi_truc(filled=False)`: ô giữa câu ("Tôi tên là ……, sinh ngày …/…/……"), hai ô trên một dòng, ô tick,
  ô nhiều dòng dưới "Lý do:", bảng nhãn–ô trống, mục lab ghi, bảng chữ ký. `filled=True`: cùng mẫu đã điền thông tin.
- `de_nghi_thanh_toan()`: nhãn lặp ở hai mục, ô tick xuống dòng, bảng STT, file đính kèm.
"""

from __future__ import annotations

import io

from docx import Document

from app.forms.schema_gen.blanks import walk

D = "……………………………"
NAME = "Nguyễn Văn Mẫu"


def _bytes(doc: object) -> bytes:
    out = io.BytesIO()
    doc.save(out)  # type: ignore[attr-defined]
    return out.getvalue()


def don_nghi_truc(filled: bool = False) -> bytes:
    doc = Document()
    doc.add_paragraph("CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM")
    doc.add_paragraph("Độc lập - Tự do - Hạnh phúc")
    doc.add_paragraph(
        "Hà Nội, ngày 20 tháng 10 năm 2026" if filled else "Hà Nội, ngày ..... tháng ..... năm ....."
    )
    doc.add_paragraph("ĐƠN XIN NGHỈ TRỰC LAB")
    doc.add_paragraph("Kính gửi: Leader phòng thí nghiệm")
    p = doc.add_paragraph()
    if filled:
        p.add_run("Tôi tên là ")
        p.add_run(NAME).bold = True
        p.add_run(", sinh ngày 01/02/2004")
    else:
        p.add_run("Tôi tên là ")
        p.add_run(D).bold = True
        p.add_run(", sinh ngày ……/……/………")
    doc.add_paragraph(
        "Email: mau@gmail.com    Điện thoại: 0912345678" if filled else f"Email: {D}    Điện thoại: {D}"
    )
    doc.add_paragraph("Buổi xin nghỉ: ☒ Sáng ☐ Chiều" if filled else "Buổi xin nghỉ: ☐ Sáng ☐ Chiều")
    doc.add_paragraph("Số buổi nghỉ: 2" if filled else f"Số buổi nghỉ: {D}")
    doc.add_paragraph("Lý do:")
    if filled:
        doc.add_paragraph("Về quê có việc gia đình.")
    else:
        doc.add_paragraph(D * 3)
        doc.add_paragraph(D * 3)
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Ngày xin nghỉ", "25/10/2026" if filled else ""
    t.cell(1, 0).text, t.cell(1, 1).text = "Người trực thay", "Trần Thị B" if filled else ""
    doc.add_paragraph("II. Ý KIẾN CỦA LEADER")
    doc.add_paragraph(f"Đồng ý / không đồng ý: {D}")
    s = doc.add_table(rows=2, cols=2)
    s.cell(0, 0).text = "XÁC NHẬN CỦA LEADER\n(Ký và ghi rõ họ tên)"
    s.cell(0, 1).text = "NGƯỜI LÀM ĐƠN\n(Ký và ghi rõ họ tên)"
    s.cell(1, 1).text = NAME if filled else ""
    return _bytes(doc)


def de_nghi_thanh_toan() -> bytes:
    doc = Document()
    doc.add_paragraph("GIẤY ĐỀ NGHỊ THANH TOÁN")
    doc.add_paragraph("I. NGƯỜI ĐỀ NGHỊ")
    doc.add_paragraph(f"Họ và tên: {D}")
    doc.add_paragraph(f"Số điện thoại: {D}")
    doc.add_paragraph("II. NGƯỜI NHẬN TIỀN")
    doc.add_paragraph(f"Họ và tên: {D}")
    doc.add_paragraph(f"Số điện thoại: {D}")
    doc.add_paragraph("Nội dung thanh toán:")
    doc.add_paragraph(D * 3)
    doc.add_paragraph("Hình thức nhận tiền:")
    doc.add_paragraph("☐ Tiền mặt")
    doc.add_paragraph("☐ Chuyển khoản")
    doc.add_paragraph("III. DANH SÁCH KHOẢN CHI")
    t = doc.add_table(rows=3, cols=4)
    for i, h in enumerate(("STT", "Nội dung chi", "Số tiền", "Ghi chú")):
        t.cell(0, i).text = h
    doc.add_paragraph("Chứng từ đính kèm:")
    s = doc.add_table(rows=2, cols=2)
    s.cell(0, 0).text = "NGƯỜI ĐỀ NGHỊ\n(Ký, ghi rõ họ tên)"
    s.cell(0, 1).text = "KẾ TOÁN\n(Ký, ghi rõ họ tên)"
    return _bytes(doc)


def para_id(data: bytes, contains: str, nth: int = 0) -> str:
    """Mã đoạn văn (như LLM nhìn thấy) của đoạn thứ `nth` chứa `contains`."""
    hits = [pa.idx for pa in walk(Document(io.BytesIO(data))) if contains in pa.text]
    return f"p{hits[nth]}"


def filled_analysis(data: bytes) -> dict[str, object]:
    """Kết quả LLM (kịch bản) cho `don_nghi_truc(filled=True)`: thông tin mẫu cần tẩy + danh sách trường."""

    def span(contains: str, text: str, nth: int = 0) -> dict[str, str]:
        return {"para": para_id(data, contains, nth), "text": text}

    def fld(
        anchor: str, key: str, label: str, type_: str, blank: str = "inline", **kw: object
    ) -> dict[str, object]:
        base: dict[str, object] = {
            "candidate": None,
            "anchor": anchor,
            "blank": blank,
            "key": key,
            "label": label,
            "type": type_,
            "required": True,
            "filled_by": "user",
            "options": [],
            "multiple": False,
            "hint": None,
            "columns": [],
        }
        base.update(kw)
        return base

    return {
        "title": "Đơn xin nghỉ trực",
        "description": "Thành viên xin nghỉ buổi trực lab, có người trực thay.",
        "is_filled_sample": True,
        "sample_values": [
            span("Tôi tên là", NAME),
            span("Tôi tên là", "01/02/2004"),
            span("Email:", "mau@gmail.com"),
            span("Email:", "0912345678"),
            span("Số buổi nghỉ", "2"),
            span("Về quê", "Về quê có việc gia đình."),
            span("25/10/2026", "25/10/2026"),
            span("Trần Thị B", "Trần Thị B"),
            span(NAME, NAME, 1),  # tên dưới ô ký
        ],
        "fields": [
            fld("Hà Nội, ngày", "ngay_lap", "Ngày làm đơn", "date", "date_parts", filled_by="system"),
            fld("Tôi tên là", "ho_ten", "Họ và tên", "text", "ellipsis"),
            fld("sinh ngày", "ngay_sinh", "Ngày sinh", "date", "ellipsis", required=False),
            fld("Email:", "email", "Email", "email"),
            fld("Điện thoại:", "sdt", "Điện thoại", "phone"),
            fld("Buổi xin nghỉ:", "buoi", "Buổi xin nghỉ", "choice", "checkbox", options=["Sáng", "Chiều"]),
            fld("Số buổi nghỉ:", "so_buoi", "Số buổi nghỉ", "int"),
            fld("Lý do:", "ly_do", "Lý do xin nghỉ", "longtext", "next_line"),
            fld("Ngày xin nghỉ", "ngay_nghi", "Ngày xin nghỉ", "date", "cell"),
            fld("Người trực thay", "nguoi_truc_thay", "Người trực thay", "text", "cell", required=False),
            fld(
                "Đồng ý / không đồng ý:",
                "y_kien",
                "Ý kiến của leader",
                "text",
                filled_by="lab",
                required=False,
            ),
        ],
        "ignore": [],
        "signatures": [
            {"label": "XÁC NHẬN CỦA LEADER", "signer_key": None},
            {"label": "NGƯỜI LÀM ĐƠN", "signer_key": "ho_ten"},
        ],
        "suggested_roles": ["member", "thu_ki", "leader"],
        "notes": ["Đơn liên quan lịch trực: hệ thống chỉ lưu đơn, không tự sửa lịch trực."],
    }
