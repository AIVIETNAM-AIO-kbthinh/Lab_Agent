"""SheetTable: đọc/ghi sheet theo TIÊU ĐỀ CỘT (mục 7.3).

- So khớp tiêu đề sau khi chuẩn hoá NFC, gộp khoảng trắng, casefold.
- Cột không khai trong config (vd Address) không bao giờ được đọc vào Row.
- Ghi từng ô, không ghi đè cả dòng.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.errors import HeaderMissing
from app.google.sheets import SheetsAPI
from app.sheets.a1 import cell, col_letter, first_row_of, quote_tab


def norm_header(s: Any) -> str:
    text = unicodedata.normalize("NFC", str(s if s is not None else ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def cell_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return unicodedata.normalize("NFC", str(v)).strip()


@dataclass
class Row:
    row_number: int
    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        v = self.values.get(key, default)
        return default if v == "" else v

    def text(self, key: str) -> str:
        return cell_str(self.values.get(key))

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


class SheetTable:
    def __init__(
        self,
        api: SheetsAPI,
        spreadsheet_id: str,
        tab: str | None,
        columns: dict[str, str],
        header_row: int = 1,
        optional: Iterable[str] = (),
    ) -> None:
        self.api = api
        self.spreadsheet_id = spreadsheet_id
        self.tab = tab
        self.columns = columns
        self.header_row = header_row
        self.optional = set(optional)
        self.header_map: dict[str, int] = {}  # logical -> cột 1-based
        self.missing_optional: list[str] = []
        self._rows: list[Row] = []
        self._loaded = False

    async def resolve_tab(self) -> str:
        if self.tab is None:
            tabs = await self.api.get_tabs(self.spreadsheet_id)
            if not tabs:
                raise HeaderMissing(f"Spreadsheet {self.spreadsheet_id} không có tab nào")
            self.tab = tabs[0].title
        return self.tab

    async def load(self) -> SheetTable:
        tab = await self.resolve_tab()
        values = await self.api.get_values(self.spreadsheet_id, f"{quote_tab(tab)}!A{self.header_row}:ZZ")
        header = values[0] if values else []
        by_norm: dict[str, int] = {}
        for i, h in enumerate(header):
            key = norm_header(h)
            if key and key not in by_norm:
                by_norm[key] = i + 1
        self.header_map = {}
        missing: list[str] = []
        self.missing_optional = []
        for logical, title in self.columns.items():
            idx = by_norm.get(norm_header(title))
            if idx is None:
                (self.missing_optional if logical in self.optional else missing).append(title)
            else:
                self.header_map[logical] = idx
        if missing:
            raise HeaderMissing(
                f"Tab '{tab}' (dòng {self.header_row}) thiếu tiêu đề: {', '.join(missing)}",
                user_message="Dữ liệu trên sheet đang thiếu cột, admin cần kiểm tra lại.",
            )
        self._rows = []
        for offset, raw in enumerate(values[1:], start=1):
            vals = {k: (raw[c - 1] if c - 1 < len(raw) else "") for k, c in self.header_map.items()}
            if all(cell_str(v) == "" for v in vals.values()):
                continue
            self._rows.append(Row(self.header_row + offset, vals))
        self._loaded = True
        return self

    def rows(self) -> list[Row]:
        if not self._loaded:
            raise RuntimeError("SheetTable.load() chưa được gọi")
        return list(self._rows)

    def has(self, logical: str) -> bool:
        return logical in self.header_map

    def find(self, **criteria: Any) -> list[Row]:
        out = []
        for r in self.rows():
            if all(norm_header(r.get(k, "")) == norm_header(v) for k, v in criteria.items()):
                out.append(r)
        return out

    def _to_row_list(self, values: dict[str, Any]) -> list[Any]:
        unknown = set(values) - set(self.header_map)
        if unknown:
            raise KeyError(f"Cột không có trên sheet: {sorted(unknown)}")
        width = max(self.header_map.values())
        row: list[Any] = [""] * width
        for k, v in values.items():
            row[self.header_map[k] - 1] = v
        return row

    async def append(self, values: dict[str, Any]) -> int:
        rows = await self.append_many([values])
        return rows[0]

    async def append_many(self, items: list[dict[str, Any]]) -> list[int]:
        if not items:
            return []
        tab = await self.resolve_tab()
        width = max(self.header_map.values())
        rng = f"{quote_tab(tab)}!A{self.header_row}:{col_letter(width)}"
        updated = await self.api.append_values(
            self.spreadsheet_id, rng, [self._to_row_list(v) for v in items]
        )
        first = first_row_of(updated)
        if first is None:
            await self.load()
            return [r.row_number for r in self._rows[-len(items) :]]
        new_rows = [first + i for i in range(len(items))]
        for rn, v in zip(new_rows, items, strict=True):
            self._rows.append(Row(rn, {k: v.get(k, "") for k in self.header_map}))
        return new_rows

    async def update(self, row_number: int, values: dict[str, Any]) -> None:
        tab = await self.resolve_tab()
        unknown = set(values) - set(self.header_map)
        if unknown:
            raise KeyError(f"Cột không có trên sheet: {sorted(unknown)}")
        data = [(cell(tab, self.header_map[k], row_number), [[v]]) for k, v in values.items()]
        await self.api.batch_update_values(self.spreadsheet_id, data)
        for r in self._rows:
            if r.row_number == row_number:
                r.values.update(values)
