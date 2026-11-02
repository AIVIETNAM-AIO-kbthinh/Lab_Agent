"""Hook `application` — đơn xin gia nhập (TK §8.1).

Folder submissions/applications/<HoTen>_<SDT>/ (cùng SĐT nộp lại → cùng folder, file thêm _v2…),
dòng FormSubmissions/don_xin_gia_nhap với trang_thai = CHO_DUYET, link_file = link folder.
Leader thấy đơn mới trong báo cáo chủ nhật.
"""

from __future__ import annotations

from typing import Any

from app.db.models import Draft
from app.forms.schema import FormSchema
from app.hooks.base import Hook, register_hook


def _message(schema: FormSchema, d: Draft, results: dict[str, Any]) -> str:
    email = (d.fields_json or {}).get("email") or ""
    return "Đơn xin gia nhập của bạn đã được gửi tới lab. Lab sẽ xem xét và liên hệ với bạn" + (
        f" qua email {email}." if email else "."
    )


HOOK = register_hook(Hook("application", message=_message, link_folder=True, status="CHO_DUYET"))
