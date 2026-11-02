"""③ Suy luận kiểu dữ liệu + key từ nhãn (luật trước; LLM xử lý phần còn lại — TK §7.7).

| Dấu hiệu                                   | Kiểu                                   |
|--------------------------------------------|----------------------------------------|
| "điện thoại", "SĐT"                        | phone                                  |
| "email", "thư điện tử"                     | email                                  |
| "ngày", "hạn …", mẫu `…/…/……`, `ngày … tháng … năm …` | date                        |
| "giờ", "lúc", "thời gian bắt đầu/kết thúc" | time                                   |
| "số lượng", "SL", "số ngày/buổi/người"     | int                                    |
| "thiết bị" (cột bảng)                      | equipment_ref (+ cột mã/tình trạng tự điền) |
| `☐ A ☐ B`                                  | choice với options                     |
| "chữ ký", "xác nhận của…", "ý kiến của…", mục lab ghi | filled_by: lab              |
| "lý do", "nội dung", "mô tả", "ghi chú", dòng chấm | longtext ("ghi chú", "lý do" không bắt buộc) |
| "CV", "hồ sơ", "đính kèm" (không có dòng chấm) | attachment                         |
| còn lại                                    | text, bắt buộc                         |
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.names import ascii_key

# key chuẩn: hook/đặt tên file dùng `ho_ten`, `sdt`; đơn chỉ cho thành viên lấy các trường này từ tài khoản
STD_KEYS: list[tuple[tuple[str, ...], str]] = [
    (("ho va ten", "ho ten", "ho, ten", "ten la", "toi ten", "em ten"), "ho_ten"),
    (("email", "e-mail", "thu dien tu"), "email"),
    (("dien thoai", "sdt", "so dt", "di dong"), "sdt"),
    (("ma so sinh vien", "mssv", "ma sinh vien", "ma sv"), "mssv"),
    (("ngay sinh", "sinh ngay"), "ngay_sinh"),
    (("vai tro",), "vai_tro"),
]
# key chuẩn → nguồn trong tài khoản thành viên (Members) khi đơn không dành cho guest
ACCOUNT_KEYS = {"ho_ten": "ho_ten", "email": "email", "sdt": "sdt", "mssv": "mssv", "vai_tro": "role_label"}
RESERVED = {
    "today",
    "ma_phieu",
    "submission_id",
    "ho_ten_ascii",
    "folder_name",
    "r",
    "loop",
    "and",
    "or",
    "not",
    "in",
    "is",
    "if",
    "else",
    "for",
    "true",
    "false",
    "none",
}
LAB_LABEL = ("chu ky", "xac nhan cua", "y kien cua", "phe duyet", "danh cho", "nhan xet cua", "duyet cua")
ATTACH = ("cv", "so yeu ly lich", "dinh kem", "ho so", "minh chung", "tep", "file", "ban sao")
LONG = (
    "ly do",
    "noi dung",
    "mo ta",
    "ghi chu",
    "y kien",
    "nhan xet",
    "de xuat",
    "ket luan",
    "tom tat",
    "kinh nghiem",
)
OPTIONAL = ("ghi chu", "ly do", "neu co", "khong bat buoc")
MEMBER_REF: dict[str, dict[str, Any]] = {
    "chu tri": {"allow_admin": True, "default": "leader"},
    "thu ky": {"default": "account.ho_ten"},
    "thanh vien tham du": {"multiple": True, "allow_unknown": True, "allow_admin": True},
    "nguoi tham du": {"multiple": True, "allow_unknown": True, "allow_admin": True},
    "vang mat": {"multiple": True, "required": False},
    "nguoi phu trach": {},
}


def _k(label: str) -> str:
    return re.sub(r"\(.*?\)", " ", ascii_key(label)).strip()


def has(label: str, words: tuple[str, ...]) -> bool:
    k = _k(label)
    toks = re.findall(r"[a-z0-9]+", k)
    joined = " ".join(toks)
    return any(
        (re.search(rf"\b{re.escape(w)}\b", joined) if " " not in w and len(w) <= 3 else w in k) for w in words
    )


def key_from_label(label: str, max_words: int = 5) -> str:
    words = re.findall(r"[a-z0-9]+", _k(label))[:max_words]
    key = "_".join(words)[:40].strip("_") or "truong"
    if key[0].isdigit():
        key = "f_" + key
    return key + "_" if key in RESERVED else key


def std_key(label: str) -> str | None:
    k = _k(label)
    if len(re.findall(r"[a-z0-9]+", k)) > 7:
        return None
    for pats, key in STD_KEYS:
        if any(p in k for p in pats):
            return key
    return None


def member_ref_rule(label: str) -> dict[str, Any] | None:
    k = " ".join(re.findall(r"[a-z]+", _k(label)))
    return MEMBER_REF.get(k)


def infer_field(
    label: str,
    kind: str,
    *,
    type_hint: str | None = None,
    options: list[str] | None = None,
    lab: bool = False,
    multiline: bool = False,
    system_date: bool = False,
) -> dict[str, Any]:
    """Thuộc tính FieldSpec (trừ key/anchor/blank) suy từ nhãn + kiểu ô trống."""
    out: dict[str, Any] = {"label": label, "type": "text", "required": True, "filled_by": "user"}
    if kind == "table":
        out["type"] = "table"
    elif kind == "checkbox" and options:
        out.update(type="choice", options=list(options))
    elif kind == "date_parts" or type_hint == "date":
        out["type"] = "date"
        if system_date:
            out.update(filled_by="system", default="today")
    elif kind == "after_anchor" and has(label, ATTACH):
        out.update(type="attachment", accept=["pdf", "docx"])
    elif has(label, ("email", "e-mail", "thu dien tu")):
        out["type"] = "email"
    elif has(label, ("dien thoai", "sdt", "so dt", "di dong")):
        out["type"] = "phone"
    elif has(label, ("so luong", "sl", "so ngay", "so buoi", "so nguoi", "so gio", "so tiet")):
        out["type"] = "int"
    elif has(label, ("thoi gian bat dau", "thoi gian ket thuc", "gio bat dau", "gio ket thuc", "gio", "luc")):
        out["type"] = "time"
    elif has(label, ("ngay", "han tra", "thoi han", "han hoan thanh", "han nop")):
        out["type"] = "date"
    elif (rule := member_ref_rule(label)) is not None:
        out.update(type="member_ref", **rule)
    elif kind == "next_line" or multiline or has(label, LONG):
        out["type"] = "longtext"
    if has(label, OPTIONAL):
        out["required"] = False
    if lab or has(label, LAB_LABEL):
        out.update(filled_by="lab", required=False)
        out.pop("default", None)
    return out


def infer_column(label: str, siblings: list[str]) -> dict[str, Any]:
    """Thuộc tính ColumnSpec cho cột bảng (STT → row_number; thiết bị → equipment_ref + cột tự điền)."""
    k = _k(label)
    if k in ("stt", "tt", "so tt", "so thu tu"):
        return {"key": "stt", "label": label, "type": "row_number"}
    has_eq = any(has(s, ("thiet bi",)) for s in siblings)
    if has(label, ("ten thiet bi", "thiet bi")) and not has(label, ("ma", "serial")):
        return {"key": "ten_tb", "label": label, "type": "equipment_ref"}
    if has_eq and has(label, ("ma", "serial", "so hieu")):
        return {"key": "ma_tb", "label": label, "type": "text", "derived_from": "ten_tb.ma_tb"}
    if has_eq and has(label, ("tinh trang",)) and not has(label, ("tra",)):
        return {"key": "tinh_trang", "label": label, "type": "text", "derived_from": "ten_tb.tinh_trang"}
    out: dict[str, Any] = {"key": key_from_label(label, 4), "label": label, "type": "text", "required": True}
    if has(label, ("so luong", "sl")):
        out.update(key="so_luong", type="int", min=1, default=1)
    elif has(label, ("ngay", "han")):
        out["type"] = "date"
    elif (rule := member_ref_rule(label)) is not None:
        out.update(
            type="member_ref", **{k2: v for k2, v in rule.items() if k2 in ("multiple", "allow_unknown")}
        )
    # cột ngày trong bảng thường để trống được (vd hạn hoàn thành của từng việc)
    out["required"] = not (has(label, OPTIONAL) or out["type"] == "date")
    return out
