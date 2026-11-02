"""Ghi `Table` ra Excel (xlsx) hoặc CSV.

Dữ liệu lấy từ sheet là do người dùng nhập: chuỗi bắt đầu bằng = + - @ không được thành công thức
(CSV/formula injection). Excel: ép kiểu chuỗi; CSV: thêm dấu ' phía trước.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.domain.dates import fmt_date
from app.exports.build import Column, Table

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV = "text/csv; charset=utf-8"
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")


def _sheet_title(s: str) -> str:
    for ch in "[]:*?/\\":
        s = s.replace(ch, " ")
    return s.strip()[:31] or "Data"


def _put(ws: Any, row: int, col: int, value: Any) -> Any:
    c = ws.cell(row=row, column=col)
    if isinstance(value, str):
        c.value = ILLEGAL_CHARACTERS_RE.sub("", value)
        c.data_type = "s"  # không bao giờ là công thức
    else:
        c.value = value
    return c


def to_xlsx(t: Table, *, actor: str = "") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = _sheet_title(t.title)
    for j, col in enumerate(t.columns, start=1):
        c = _put(ws, 1, j, col.label)
        c.font = Font(bold=True)
        c.fill = _HEADER_FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = col.width
    wrap = Alignment(vertical="top", wrap_text=True)
    for i, r in enumerate(t.rows, start=2):
        for j, col in enumerate(t.columns, start=1):
            v = r.get(col.key)
            if v is None or v == "":
                continue
            c = _put(ws, i, j, v)
            if col.kind == "date" and isinstance(v, date):
                c.number_format = "DD/MM/YYYY"
            if isinstance(v, str) and "\n" in v:
                c.alignment = wrap
    ws.freeze_panes = "A2"
    if t.rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(t.columns))}{len(t.rows) + 1}"

    info = wb.create_sheet("Thông tin")
    lines: list[tuple[str, Any]] = [
        ("Dữ liệu", t.title),
        ("Số dòng", len(t.rows)),
        *((f"Lọc: {k}", v) for k, v in t.filters.items()),
        ("Xuất lúc", t.created_at.strftime("%d/%m/%Y %H:%M")),
    ]
    if actor:
        lines.append(("Người xuất", actor))
    for i, (k, v) in enumerate(lines, start=1):
        _put(info, i, 1, k).font = Font(bold=True)
        _put(info, i, 2, v)
    info.column_dimensions["A"].width = 20
    info.column_dimensions["B"].width = 50

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv_cell(col: Column, v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, date):
        return fmt_date(v)
    s = str(v)
    if isinstance(v, str) and s.startswith(_FORMULA_START):
        return "'" + s
    return s


def to_csv(t: Table) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([c.label for c in t.columns])
    for r in t.rows:
        w.writerow([_csv_cell(c, r.get(c.key)) for c in t.columns])
    return buf.getvalue().encode("utf-8-sig")  # BOM: Excel mở đúng tiếng Việt


def render(t: Table, fmt: str, *, actor: str = "") -> tuple[bytes, str, str]:
    """Trả (bytes, mime, đuôi file)."""
    if fmt == "csv":
        return to_csv(t), CSV, "csv"
    return to_xlsx(t, actor=actor), XLSX, "xlsx"
