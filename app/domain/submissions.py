"""data/FormSubmissions: mỗi loại đơn một tab, tên tab = form_id (TK §3.4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.config import PathOnlyCfg
from app.domain.dates import parse_sheet_date
from app.google.paths import PathResolver
from app.google.sheets import SheetsAPI
from app.sheets.a1 import col_letter, quote_tab
from app.sheets.table import Row, SheetTable

FIXED_HEAD = ["submission_id", "thoi_gian_nop", "nguoi_nop"]
FIXED_TAIL = ["link_file", "trang_thai", "ghi_chu"]


@dataclass
class Submission:
    row_number: int
    form_id: str
    values: dict[str, Any]

    @property
    def submitted_on(self) -> date | None:
        return parse_sheet_date(self.values.get("thoi_gian_nop"))

    def text(self, key: str) -> str:
        v = self.values.get(key)
        return "" if v is None else str(v).strip()


class SubmissionsRepo:
    def __init__(self, sheets: SheetsAPI, paths: PathResolver, cfg: PathOnlyCfg) -> None:
        self.sheets = sheets
        self.paths = paths
        self.cfg = cfg

    async def _sid(self) -> str:
        return await self.paths.file_id(self.cfg.path)

    async def form_tabs(self) -> list[str]:
        return [t.title for t in await self.sheets.get_tabs(await self._sid()) if not t.title.startswith("_")]

    async def ensure_tab(self, form_id: str, field_keys: list[str]) -> None:
        """Tạo tab theo schema; tab đã có mà thiếu cột (schema thêm trường) thì thêm cột vào cuối."""
        sid = await self._sid()
        wanted = [*FIXED_HEAD, *field_keys, *FIXED_TAIL]
        if form_id not in {t.title for t in await self.sheets.get_tabs(sid)}:
            await self.sheets.batch_update(sid, [{"addSheet": {"properties": {"title": form_id}}}])
            await self.sheets.update_values(sid, f"{quote_tab(form_id)}!A1", [wanted], raw=True)
            return
        header = await self.sheets.get_values(sid, f"{quote_tab(form_id)}!A1:ZZ1")
        have = [str(h).strip() for h in (header[0] if header else [])]
        while have and not have[-1]:
            have.pop()
        missing = [k for k in wanted if k not in have]
        if missing:
            await self.sheets.update_values(
                sid, f"{quote_tab(form_id)}!{col_letter(len(have) + 1)}1", [missing], raw=True
            )

    async def _table(self, form_id: str) -> SheetTable:
        sid = await self._sid()
        header = await self.sheets.get_values(sid, f"{quote_tab(form_id)}!A1:ZZ1")
        cols = {str(h).strip(): str(h).strip() for h in (header[0] if header else []) if str(h).strip()}
        return await SheetTable(self.sheets, sid, form_id, cols, 1).load()

    async def list(self, form_id: str) -> list[Submission]:
        if form_id not in await self.form_tabs():
            return []
        t = await self._table(form_id)
        return [Submission(r.row_number, form_id, dict(r.values)) for r in t.rows()]

    async def append(self, form_id: str, values: dict[str, Any]) -> int:
        t = await self._table(form_id)
        sub_id = str(values.get("submission_id", ""))
        if sub_id and any(r.text("submission_id") == sub_id for r in t.rows()):
            return next(r.row_number for r in t.rows() if r.text("submission_id") == sub_id)
        return await t.append({k: v for k, v in values.items() if t.has(k)})

    async def set_status(self, form_id: str, submission_id: str, status: str, note: str | None = None) -> Row:
        t = await self._table(form_id)
        row = next(r for r in t.rows() if r.text("submission_id") == submission_id)
        vals: dict[str, Any] = {"trang_thai": status}
        if note is not None and t.has("ghi_chu"):
            vals["ghi_chu"] = note
        await t.update(row.row_number, vals)
        return row
