"""FakeSheets: lưu lưới ô trong bộ nhớ, mô phỏng các method Sheets mà app dùng."""

from __future__ import annotations

import copy
import itertools
import re
from dataclasses import dataclass, field
from typing import Any

from app.google.sheets import SheetProps
from app.sheets.a1 import col_index, col_letter

_RANGE = re.compile(r"^(?:'((?:[^']|'')+)'|([^!]+))!([A-Z]+)(\d+)?(?::([A-Z]+)(\d+)?)?$")
_ids = itertools.count(1000)


@dataclass
class Tab:
    title: str
    sheet_id: int
    index: int
    grid: list[list[Any]] = field(default_factory=list)


def parse_a1(a1: str) -> tuple[str, int, int, int | None, int | None]:
    m = _RANGE.match(a1)
    if not m:
        raise ValueError(f"A1 không hợp lệ: {a1}")
    tab = (m.group(1) or "").replace("''", "'") if m.group(1) else m.group(2)
    c1, r1 = col_index(m.group(3)), int(m.group(4) or 1)
    c2 = col_index(m.group(5)) if m.group(5) else None
    r2 = int(m.group(6)) if m.group(6) else None
    if m.group(5) is None:
        c2, r2 = c1, r1
    return tab, c1, r1, c2, r2


class FakeSheets:
    def __init__(self) -> None:
        self.books: dict[str, list[Tab]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.protected: list[dict[str, Any]] = []
        self.fail_next: list[Exception] = []

    # ---------------------------------------------------------------- dựng dữ liệu
    def add_tab(self, sid: str, title: str, rows: list[list[Any]]) -> Tab:
        tabs = self.books.setdefault(sid, [])
        t = Tab(title, next(_ids), len(tabs), [list(r) for r in rows])
        tabs.append(t)
        return t

    def tab(self, sid: str, title: str) -> Tab:
        return next(t for t in self.books[sid] if t.title == title)

    def values(self, sid: str, title: str) -> list[list[Any]]:
        return self.tab(sid, title).grid

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    # ---------------------------------------------------------------- API
    async def get_tabs(self, spreadsheet_id: str) -> list[SheetProps]:
        self._maybe_fail()
        return [SheetProps(t.sheet_id, t.title, t.index) for t in self.books.get(spreadsheet_id, [])]

    async def get_values(
        self, spreadsheet_id: str, a1: str, render: str = "UNFORMATTED_VALUE"
    ) -> list[list[Any]]:
        self._maybe_fail()
        tab, c1, r1, c2, r2 = parse_a1(a1)
        grid = self.tab(spreadsheet_id, tab).grid
        out = []
        last = len(grid) if r2 is None else min(r2, len(grid))
        for r in range(r1 - 1, last):
            row = grid[r]
            end = len(row) if c2 is None else min(c2, len(row))
            vals = row[c1 - 1 : end]
            while vals and vals[-1] in ("", None):
                vals = vals[:-1]
            out.append(list(vals))
        while out and not out[-1]:
            out.pop()
        return out

    def _write(self, sid: str, a1: str, values: list[list[Any]]) -> None:
        tab, c1, r1, _, _ = parse_a1(a1)
        grid = self.tab(sid, tab).grid
        for i, row in enumerate(values):
            r = r1 - 1 + i
            while len(grid) <= r:
                grid.append([])
            for j, v in enumerate(row):
                c = c1 - 1 + j
                while len(grid[r]) <= c:
                    grid[r].append("")
                grid[r][c] = v

    async def update_values(
        self, spreadsheet_id: str, a1: str, values: list[list[Any]], raw: bool = False
    ) -> None:
        self._maybe_fail()
        self.calls.append(("update", a1))
        self._write(spreadsheet_id, a1, values)

    async def batch_update_values(
        self, spreadsheet_id: str, data: list[tuple[str, list[list[Any]]]], raw: bool = False
    ) -> None:
        self._maybe_fail()
        for a1, values in data:
            self.calls.append(("update", a1))
            self._write(spreadsheet_id, a1, values)

    async def append_values(self, spreadsheet_id: str, a1: str, values: list[list[Any]]) -> str:
        self._maybe_fail()
        tab, c1, r1, _, _ = parse_a1(a1)
        grid = self.tab(spreadsheet_id, tab).grid
        last = len(grid)
        while last > 0 and all(v in ("", None) for v in grid[last - 1]):
            last -= 1
        start = max(last + 1, r1)
        while len(grid) > last:
            grid.pop()
        while len(grid) < start - 1:
            grid.append([])
        for row in values:
            grid.append([""] * (c1 - 1) + list(row))
        self.calls.append(("append", a1))
        end = start + len(values) - 1
        return f"'{tab}'!{col_letter(c1)}{start}:{col_letter(c1 + max(len(r) for r in values) - 1)}{end}"

    async def batch_update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._maybe_fail()
        replies: list[dict[str, Any]] = []
        tabs = self.books.setdefault(spreadsheet_id, [])
        for req in requests:
            ((kind, body),) = req.items()
            self.calls.append((kind, body))
            if kind == "addSheet":
                t = self.add_tab(spreadsheet_id, body["properties"]["title"], [])
                replies.append({"addSheet": {"properties": {"sheetId": t.sheet_id, "title": t.title}}})
            elif kind == "updateSheetProperties":
                t = next(x for x in tabs if x.sheet_id == body["properties"]["sheetId"])
                t.title = body["properties"].get("title", t.title)
                replies.append({})
            elif kind == "insertDimension":
                rg = body["range"]
                t = next(x for x in tabs if x.sheet_id == rg["sheetId"])
                for _ in range(rg["endIndex"] - rg["startIndex"]):
                    t.grid.insert(rg["startIndex"], [])
                replies.append({})
            elif kind == "deleteDimension":
                rg = body["range"]
                t = next(x for x in tabs if x.sheet_id == rg["sheetId"])
                del t.grid[rg["startIndex"] : rg["endIndex"]]
                replies.append({})
            elif kind == "addProtectedRange":
                self.protected.append(body)
                replies.append({})
            else:
                raise NotImplementedError(kind)
        return replies

    async def copy_sheet_to(self, spreadsheet_id: str, sheet_id: int, dest_spreadsheet_id: str) -> SheetProps:
        self._maybe_fail()
        src = next(t for t in self.books[spreadsheet_id] if t.sheet_id == sheet_id)
        t = self.add_tab(dest_spreadsheet_id, f"Copy of {src.title}", copy.deepcopy(src.grid))
        return SheetProps(t.sheet_id, t.title, t.index)
