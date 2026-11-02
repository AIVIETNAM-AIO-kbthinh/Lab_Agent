"""Schema đơn (TK §7.1). Ở P3 viết tay trong `schemas/`; ở P4 do schema_gen sinh ra cùng định dạng.

Giá trị lưu trong bản nháp (đã chuẩn hoá):
    text/longtext/phone/email/choice → str · date → "dd/mm/yyyy" · time → "HH:MM" · int → int
    member_ref → str (multiple: list[str]) · equipment_ref → tên chuẩn trong Thietbi
    table → list[dict[cột, giá trị]] · attachment → attachments_json[key] (không nằm trong fields)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

FieldType = Literal[
    "text",
    "longtext",
    "phone",
    "email",
    "date",
    "time",
    "int",
    "choice",
    "member_ref",
    "equipment_ref",
    "row_number",
    "table",
    "attachment",
]
SCALAR_TYPES = {"text", "longtext", "phone", "email", "date", "time", "int", "choice"}
ACCOUNT_SOURCES = {"ho_ten", "email", "mssv", "sdt", "lop", "truong", "role_label"}
# Vị trí ô trống so với anchor khi chèn placeholder / trích xuất từ file đã điền:
#   inline "Nhãn: ……" · next_line dòng chấm dưới nhãn · after_anchor ngay sau nhãn (không có dấu chấm)
#   date_parts "ngày … tháng … năm …" · ellipsis "…" giữa câu · cell ô bảng bên phải ô nhãn
#   checkbox "Nhãn: ☐ A ☐ B" (kiểu choice, mỗi ô tick một lựa chọn theo thứ tự `options`)
BlankKind = Literal["inline", "next_line", "after_anchor", "date_parts", "ellipsis", "cell", "checkbox"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnSpec(_M):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    type: FieldType = "text"
    required: bool = True
    default: Any = None
    hint: str | None = None  # gợi ý thêm cho LLM khi trích xuất
    min: int | None = None
    max: int | None = None
    max_length: int | None = None
    options: list[str] = []
    multiple: bool = False
    allow_admin: bool = False
    allow_unknown: bool = False
    derived_from: str | None = None  # "<cột equipment_ref>.<ma_tb|tinh_trang|ten_tb>"
    not_before: str | None = None  # "today" | key trường khác
    not_after: str | None = None
    after: str | None = None
    before: str | None = None

    @property
    def derived(self) -> bool:
        return self.derived_from is not None or self.type == "row_number"


class FieldSpec(ColumnSpec):
    anchor: str | None = None
    blank: BlankKind = "inline"
    source: str | None = None  # "account.<trường>"
    locked: bool = False
    filled_by: Literal["user", "system", "lab"] = "user"
    columns: list[ColumnSpec] = []
    min_rows: int = 0
    max_rows: int = 30
    rules: list[str] = []
    accept: list[str] = []  # attachment: ["pdf", "docx"]
    suggest: str | None = None  # "members_except:<key>" (vd vắng mặt = Members trừ người tham dự)

    @property
    def user_editable(self) -> bool:
        return self.filled_by == "user" and not self.locked

    @property
    def account_field(self) -> str | None:
        if self.source and self.source.startswith("account."):
            return self.source.split(".", 1)[1]
        return None

    def column(self, key: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.key == key), None)


class SignatureSpec(_M):
    label: str  # chữ in hoa trong ô ký, vd "NGƯỜI LÀM ĐƠN"
    field: str | None = None  # trường có họ tên in vào ô (thay chữ ký khi nộp online); None = lab ký tay


class TemplateSpec(_M):
    drive_path: str  # đơn trống gốc admin thả vào templates/ (Google Doc hoặc .docx)
    file_id: str | None = None  # P4: id file mẫu trắng do hệ thống lưu (ưu tiên hơn drive_path khi tải mẫu)


class OutputSpec(_M):
    format: Literal["pdf", "gdoc"] = "pdf"
    folder: str  # "submissions/applications/{folder_name}" (str.format với ngữ cảnh lúc lưu)
    filename: str  # không có đuôi, vd "{today:%Y-%m-%d}_DonXinGiaNhap"
    attachment_names: dict[str, str] = {}  # key đính kèm → tên file (không đuôi)
    versioning: bool = True  # trùng tên trong folder ⇒ thêm _v2, _v3


class FormSchema(_M):
    form_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}(\.\d+)?$")
    title: str
    description: str
    roles: list[Literal["guest", "member", "thu_ki", "leader", "admin"]]
    hook: Literal["application", "equipment_borrow", "meeting_minutes"] | None = None
    template: TemplateSpec
    fields: list[FieldSpec]
    signatures: list[SignatureSpec] = []
    output: OutputSpec
    intro: str | None = None  # câu mở đầu lời mời điền đơn

    @model_validator(mode="after")
    def _check(self) -> FormSchema:
        keys = [f.key for f in self.fields]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            raise ValueError(f"{self.form_id}: trùng key {sorted(dup)}")
        by_key = {f.key: f for f in self.fields}
        for f in self.fields:
            if f.type == "table":
                if not f.columns:
                    raise ValueError(f"{self.form_id}.{f.key}: table thiếu columns")
                ckeys = {c.key for c in f.columns}
                for c in f.columns:
                    if c.type in {"table", "attachment"}:
                        raise ValueError(f"{self.form_id}.{f.key}.{c.key}: kiểu {c.type} không dùng làm cột")
                    if c.derived_from:
                        base, _, attr = c.derived_from.partition(".")
                        if base not in ckeys or attr not in {"ma_tb", "tinh_trang", "ten_tb"}:
                            raise ValueError(f"{self.form_id}.{f.key}.{c.key}: derived_from sai")
            if f.account_field and f.account_field not in ACCOUNT_SOURCES:
                raise ValueError(f"{self.form_id}.{f.key}: source '{f.source}' không hỗ trợ")
            if f.type == "choice" and not f.options:
                raise ValueError(f"{self.form_id}.{f.key}: choice thiếu options")
            if f.blank == "checkbox" and f.type != "choice":
                raise ValueError(f"{self.form_id}.{f.key}: ô tick (checkbox) phải là kiểu choice")
            if f.suggest:
                kind, _, ref = f.suggest.partition(":")
                if kind != "members_except" or ref not in by_key:
                    raise ValueError(f"{self.form_id}.{f.key}: suggest sai")
            for rel in (f.not_before, f.not_after, f.after, f.before):
                if rel and rel != "today" and rel not in by_key:
                    raise ValueError(f"{self.form_id}.{f.key}: ràng buộc tham chiếu '{rel}' không tồn tại")
        for s in self.signatures:
            if s.field and s.field not in by_key:
                raise ValueError(f"{self.form_id}: chữ ký '{s.label}' trỏ tới trường không tồn tại")
        for k in self.output.attachment_names:
            if k not in by_key or by_key[k].type != "attachment":
                raise ValueError(f"{self.form_id}: output.attachment_names.{k} không phải trường attachment")
        return self

    # ----------------------------------------------------------------- tiện ích

    def field(self, key: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.key == key), None)

    def data_fields(self) -> list[FieldSpec]:
        """Trường có giá trị (bỏ attachment và trường lab ghi tay)."""
        return [f for f in self.fields if f.type != "attachment" and f.filled_by != "lab"]

    def attachments(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.type == "attachment"]

    def user_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.user_editable]

    def submission_keys(self) -> list[str]:
        """Cột của tab FormSubmissions/<form_id> (sau các cột cố định)."""
        return [f.key for f in self.data_fields()]

    def invitation(self) -> str:
        """Lời mời điền hộ: danh sách thông tin sinh từ `fields` (TK §7.2)."""
        parts = []
        for f in self.user_fields():
            if f.type == "attachment":
                continue
            parts.append(f.label.lower() + ("" if f.required else " (không bắt buộc)"))
        att = [f.label for f in self.attachments() if f.required]
        text = ", ".join(parts)
        tail = f" Đừng quên gửi kèm {', '.join(att)} nhé." if att else ""
        return f"Bạn cho mình các thông tin: {text}, mình sẽ điền đơn giúp bạn.{tail}"

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_defaults=False)


_FMT_FIELD = re.compile(r"{([a-z_]+)")


def load_schema_file(path: str | Path) -> FormSchema:
    with Path(path).open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return FormSchema.model_validate(raw)


def load_schema_dir(path: str | Path) -> dict[str, FormSchema]:
    out: dict[str, FormSchema] = {}
    for p in sorted(Path(path).glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        s = load_schema_file(p)
        if s.form_id in out:
            raise ValueError(f"Hai schema cùng form_id '{s.form_id}'")
        out[s.form_id] = s
    return out


def format_names(template: str) -> set[str]:
    """Tên biến dùng trong output.folder / filename."""
    return set(_FMT_FIELD.findall(template))
