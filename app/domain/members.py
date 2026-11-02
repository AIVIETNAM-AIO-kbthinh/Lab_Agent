"""data/Members: thành viên = bảng phân quyền (TK §3.1)."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import MembersSheetCfg
from app.domain.dates import fmt_date
from app.domain.names import fuzzy_match, norm_name, normalize_phone
from app.errors import DuplicateNames, HeaderMissing, NotFound
from app.google.paths import PathResolver
from app.google.sheets import SheetsAPI
from app.sheets.a1 import quote_tab
from app.sheets.table import Row, SheetTable

LOGICAL = ("mssv", "ho_ten", "lop", "sdt", "truong", "email", "role")


@dataclass(frozen=True)
class Member:
    row_number: int
    ho_ten: str
    email: str
    role: str  # leader | thu_ki | member
    role_raw: str = ""
    mssv: str = ""
    lop: str = ""
    sdt: str = ""
    truong: str = ""

    @property
    def key(self) -> str:
        return norm_name(self.ho_ten)


@dataclass
class MembersSnapshot:
    active: list[Member]
    former: list[Member] = field(default_factory=list)
    unknown_roles: list[tuple[str, str]] = field(default_factory=list)  # (họ tên, role_raw)

    def by_email(self, email: str) -> Member | None:
        e = (email or "").strip().casefold()
        for m in self.active:
            if m.email.casefold() == e:
                return m
        return None

    def by_name(self, name: str) -> Member | None:
        k = norm_name(name)
        for m in self.active:
            if m.key == k:
                return m
        return None

    def former_by_name(self, name: str) -> Member | None:
        k = norm_name(name)
        return next((m for m in self.former if m.key == k), None)

    def search(self, name: str, limit: int = 5) -> list[Member]:
        hits = fuzzy_match(name, [m.ho_ten for m in self.active], limit=limit)
        by = {m.ho_ten: m for m in self.active}
        return [by[h] for h, _ in hits]

    def leader(self) -> Member | None:
        return next((m for m in self.active if m.role == "leader"), None)

    def secretaries(self) -> list[Member]:
        return [m for m in self.active if m.role == "thu_ki"]

    def duplicate_names(self) -> list[str]:
        seen: dict[str, str] = {}
        dups: list[str] = []
        for m in self.active:
            if m.key in seen:
                dups.append(m.ho_ten)
            seen.setdefault(m.key, m.ho_ten)
        return dups

    def assert_unique_names(self) -> None:
        dups = self.duplicate_names()
        if dups:
            raise DuplicateNames(f"Members có họ tên trùng: {', '.join(sorted(set(dups)))}")

    def names(self) -> set[str]:
        return {m.key for m in self.active}


def _row_to_member(r: Row, role_values: dict[str, str]) -> Member | None:
    ho_ten = r.text("ho_ten")
    if not ho_ten:
        return None
    raw = r.text("role")
    role_map = {k.casefold(): v for k, v in role_values.items()}
    return Member(
        row_number=r.row_number,
        ho_ten=ho_ten,
        email=r.text("email").lower(),
        role=role_map.get(raw.casefold(), "member" if not raw else "unknown"),
        role_raw=raw,
        mssv=r.text("mssv"),
        lop=r.text("lop"),
        sdt=normalize_phone(r.text("sdt")) if r.text("sdt") else "",
        truong=r.text("truong"),
    )


class MemberDirectory:
    """Đọc Members, cache ngắn (role_cache_seconds)."""

    def __init__(
        self, sheets: SheetsAPI, paths: PathResolver, cfg: MembersSheetCfg, ttl_s: float = 60
    ) -> None:
        self.sheets = sheets
        self.paths = paths
        self.cfg = cfg
        self.ttl_s = ttl_s
        self._snap: MembersSnapshot | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    def _columns(self) -> dict[str, str]:
        return {k: v for k, v in self.cfg.columns.items() if k in LOGICAL}

    async def table(self) -> SheetTable:
        sid = await self.paths.file_id(self.cfg.path)
        return await SheetTable(
            self.sheets,
            sid,
            self.cfg.tab,
            self._columns(),
            self.cfg.header_row,
            optional=("mssv", "lop", "sdt", "truong"),
        ).load()

    async def snapshot(self, *, force: bool = False) -> MembersSnapshot:
        async with self._lock:
            if not force and self._snap is not None and time.monotonic() - self._at < self.ttl_s:
                return self._snap
            t = await self.table()
            active: list[Member] = []
            unknown: list[tuple[str, str]] = []
            for r in t.rows():
                m = _row_to_member(r, self.cfg.role_values)
                if m is None:
                    continue
                if m.role == "unknown":
                    unknown.append((m.ho_ten, m.role_raw))
                    m = Member(**{**m.__dict__, "role": "member"})
                active.append(m)
            former = await self._load_former(t.spreadsheet_id)
            self._snap = MembersSnapshot(active, former, unknown)
            self._at = time.monotonic()
            return self._snap

    async def _load_former(self, sid: str) -> list[Member]:
        tabs = {t.title for t in await self.sheets.get_tabs(sid)}
        if self.cfg.former_tab not in tabs:
            return []
        try:
            t = await SheetTable(
                self.sheets,
                sid,
                self.cfg.former_tab,
                self._columns(),
                self.cfg.header_row,
                optional=("mssv", "lop", "sdt", "truong", "email", "role"),
            ).load()
        except HeaderMissing:
            return []
        out = []
        for r in t.rows():
            m = _row_to_member(r, self.cfg.role_values)
            if m:
                out.append(m)
        return out

    def invalidate(self) -> None:
        self._snap = None

    # ------------------------------------------------------------ ghi (dùng qua outbox ở P3)

    async def append_member(self, values: dict[str, str]) -> int:
        """Thêm thành viên vào tab chính; cột "No" (nếu có) = số thứ tự tiếp theo (TK §8.5)."""
        sid = await self.paths.file_id(self.cfg.path)
        t = await SheetTable(
            self.sheets,
            sid,
            self.cfg.tab,
            {**self._columns(), "no": "No"},
            self.cfg.header_row,
            optional=("mssv", "lop", "sdt", "truong", "no"),
        ).load()
        rows = t.rows()
        if any(norm_name(r.text("ho_ten")) == norm_name(values["ho_ten"]) for r in rows):
            raise DuplicateNames(f"'{values['ho_ten']}' đã có trong Members")
        vals: dict[str, Any] = {k: v for k, v in values.items() if t.has(k)}
        if t.has("no"):
            nums = [int(float(r.text("no"))) for r in rows if re.fullmatch(r"\d+(\.0)?", r.text("no"))]
            vals["no"] = max(nums, default=0) + 1
        if vals.get("sdt"):
            vals["sdt"] = "'" + str(vals["sdt"]).lstrip("'")  # giữ số 0 đầu
        rn = await t.append(vals)
        self.invalidate()
        return rn

    async def set_role(self, ho_ten: str, role: str) -> None:
        t = await self.table()
        rows = [r for r in t.rows() if norm_name(r.text("ho_ten")) == norm_name(ho_ten)]
        if not rows:
            raise NotFound(f"Không có '{ho_ten}' trong Members")
        label = {v: k for k, v in self.cfg.role_values.items()}[role]
        await t.update(rows[0].row_number, {"role": label})
        self.invalidate()

    async def move_to_former(self, ho_ten: str, left_on: date, date_format: str = "%d/%m/%Y") -> None:
        """Chuyển dòng sang tab cựu thành viên (thêm "Ngày rời"), rồi bỏ dòng khỏi tab chính."""
        t = await self.table()
        rows = [r for r in t.rows() if norm_name(r.text("ho_ten")) == norm_name(ho_ten)]
        if not rows:
            raise NotFound(f"Không có '{ho_ten}' trong Members")
        row = rows[0]
        sid = t.spreadsheet_id
        tab = await t.resolve_tab()
        raw = await self.sheets.get_values(
            sid, f"{quote_tab(tab)}!A{row.row_number}:ZZ{row.row_number}", render="FORMATTED_VALUE"
        )
        header = await self.sheets.get_values(
            sid, f"{quote_tab(tab)}!A{self.cfg.header_row}:ZZ{self.cfg.header_row}"
        )
        tabs = {x.title: x for x in await self.sheets.get_tabs(sid)}
        if self.cfg.former_tab not in tabs:
            await self.sheets.batch_update(
                sid, [{"addSheet": {"properties": {"title": self.cfg.former_tab}}}]
            )
            await self.sheets.update_values(
                sid, f"{quote_tab(self.cfg.former_tab)}!A{self.cfg.header_row}", [[*header[0], "Ngày rời"]]
            )
        values = [*(raw[0] if raw else []), fmt_date(left_on, date_format)]
        await self.sheets.append_values(
            sid, f"{quote_tab(self.cfg.former_tab)}!A{self.cfg.header_row}", [values]
        )
        sheet_id = tabs[tab].sheet_id
        await self.sheets.batch_update(
            sid,
            [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row.row_number - 1,
                            "endIndex": row.row_number,
                        }
                    }
                }
            ],
        )
        self.invalidate()
