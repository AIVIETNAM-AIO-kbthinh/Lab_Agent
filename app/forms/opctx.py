"""Tiện ích dùng chung cho các step outbox của form (xử lý chung + hook)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from app.db.models import Draft
from app.domain.dates import parse_sheet_date
from app.domain.names import folder_name, to_pascal_ascii
from app.forms.registry import FormDef, FormRegistry
from app.outbox.steps import PreconditionFailed

if TYPE_CHECKING:
    from app.outbox.steps import StepContext


async def load_draft(ctx: StepContext) -> tuple[Draft, FormDef]:
    async with ctx.svc.sm() as s:
        d = await s.get(Draft, ctx.payload["draft_id"])
        if d is not None:
            s.expunge(d)
    if d is None:
        raise PreconditionFailed("Bản nháp không còn tồn tại.")
    fd = await FormRegistry(ctx.svc.sm).get(d.form_id, d.form_version)
    if fd is None:
        raise PreconditionFailed(f"Không còn schema {d.form_id}@{d.form_version}.")
    return d, fd


def submitted_at(d: Draft) -> datetime:
    return datetime.fromisoformat((d.result_json or {})["submitted_at"])


def naming_vars(fd: FormDef, d: Draft) -> dict[str, Any]:
    """Biến cho output.folder / filename: today (ngày nộp), ma_phieu, folder_name, ho_ten_ascii, các trường."""
    f = d.fields_json or {}
    res = d.result_json or {}
    v: dict[str, Any] = {}
    for spec in fd.schema.data_fields():
        val = f.get(spec.key)
        if spec.type == "date":
            v[spec.key] = parse_sheet_date(val) if val else date.min
        elif spec.type in ("table",):
            continue
        else:
            v[spec.key] = val if val is not None else ""
    ho_ten = str(f.get("ho_ten") or "")
    v.update(
        {
            "today": submitted_at(d).date(),
            "ma_phieu": res.get("ma_phieu", ""),
            "submission_id": res.get("submission_id", ""),
            "ho_ten_ascii": to_pascal_ascii(ho_ten) or "NguoiNop",
            "folder_name": folder_name(ho_ten, str(f.get("sdt") or "")) if ho_ten else "KhongTen",
        }
    )
    return v


def safe_name(s: str) -> str:
    """Tên file/folder trên Drive: bỏ ký tự gây rối đường dẫn."""
    return re.sub(r"[\\/:*?\"<>|\n\r\t]+", "-", s).strip(" .-") or "file"


def sheet_value(v: Any) -> Any:
    """Giá trị ghi FormSubmissions (USER_ENTERED): giữ số 0 đầu của SĐT, bảng → JSON, danh sách → chuỗi."""
    if v is None:
        return ""
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, str) and re.fullmatch(r"0\d{6,}", v):
        return "'" + v
    if isinstance(v, str) and v.startswith(("=", "+", "@")):
        return "'" + v  # chặn công thức do người dùng nhập
    return v
