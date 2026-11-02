"""data/DutySchedule: một tab / tuần, bố cục dò theo tiêu đề dòng 4–5 (TK §3.3, §9.1).

- Dòng thành viên = cột No là số và cột Name không trống.
- Cột buổi trực = các cột có "Sáng"/"Chiều" ở dòng slot, gán vào thứ gần nhất bên trái ở dòng thứ.
- Cột số buổi và nội dung công việc dò theo tiêu đề ở dòng thứ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.config import DutySheetCfg
from app.domain.dates import parse_tab_name, tab_name
from app.domain.names import ascii_key, norm_name
from app.errors import HeaderMissing
from app.google.paths import PathResolver
from app.google.sheets import SheetProps, SheetsAPI
from app.sheets.a1 import col_index, col_letter, parse_cell, quote_tab
from app.sheets.table import cell_str, norm_header

_DAY_WORDS = {
    "2": 0,
    "hai": 0,
    "3": 1,
    "ba": 1,
    "4": 2,
    "tu": 2,
    "5": 3,
    "nam": 3,
    "6": 4,
    "sau": 4,
    "7": 5,
    "bay": 5,
}


DAY_NAMES = ("Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật")


def day_text(day: int, start: date | None) -> str:
    """0 → "Thứ 2 (02/11)" (có ngày nếu biết thứ 2 của tuần)."""
    name = DAY_NAMES[day]
    return f"{name} ({start + timedelta(days=day):%d/%m})" if start else name


def _day_index(text: str) -> int | None:
    """ "Thứ 2" / "THỨ HAI" / "T2" / "Chủ nhật" / "CN" -> 0..6."""
    t = ascii_key(text)
    m = re.match(r"^(?:thu\s*|t)(\d|hai|ba|tu|nam|sau|bay)\b", t)
    if m:
        return _DAY_WORDS[m.group(1)]
    if t.startswith("chu nhat") or t == "cn":
        return 6
    return None


def _is_number(v: Any) -> bool:
    s = cell_str(v)
    try:
        float(s)
        return bool(s)
    except ValueError:
        return False


@dataclass(frozen=True)
class Slot:
    col: int  # 1-based
    day: int  # 0 = thứ 2
    label: str  # "Sáng" / "Chiều"

    @property
    def key(self) -> tuple[int, str]:
        return self.day, norm_header(self.label)


@dataclass
class DutyRow:
    row_number: int
    no: str
    name: str
    marks: dict[int, str] = field(default_factory=dict)  # col -> giá trị ô

    @property
    def key(self) -> str:
        return norm_name(self.name)


@dataclass
class DutyTab:
    title: str
    sheet_id: int
    start: date | None
    end: date | None
    slots: list[Slot]
    rows: list[DutyRow]
    count_col: int | None
    worklog_col: int | None
    numbered_rows: list[int]  # các dòng có số thứ tự (kể cả chưa có tên)

    def count_sessions(self, row: DutyRow, marks: list[str]) -> int:
        valid = {m.casefold() for m in marks}
        return sum(1 for s in self.slots if cell_str(row.marks.get(s.col)).casefold() in valid)

    def fixed_marks(self, row: DutyRow, fixed: str) -> list[Slot]:
        return [s for s in self.slots if cell_str(row.marks.get(s.col)).casefold() == fixed.casefold()]

    def marked_slots(self, row: DutyRow, marks: list[str]) -> list[Slot]:
        valid = {m.casefold() for m in marks}
        return [s for s in self.slots if cell_str(row.marks.get(s.col)).casefold() in valid]

    def slot_text(self, slot: Slot) -> str:
        """ "Thứ 2 (02/11) Sáng"."""
        return f"{day_text(slot.day, self.start)} {slot.label}"

    def roster(self, marks: list[str], fixed: str) -> list[dict[str, Any]]:
        """Lịch theo ngày: [{label, slots: [{label, people: ["A (T)", "B"]}]}]; ô T ghi kèm "(T)"."""
        valid = {m.casefold() for m in marks}
        days: list[dict[str, Any]] = []
        for d in sorted({s.day for s in self.slots}):
            slots = []
            for s in (x for x in self.slots if x.day == d):
                people = []
                for r in self.rows:
                    v = cell_str(r.marks.get(s.col)).casefold()
                    if v in valid:
                        people.append(f"{r.name} ({fixed})" if v == fixed.casefold() else r.name)
                slots.append({"label": s.label, "people": people})
            days.append({"label": day_text(d, self.start), "slots": slots})
        return days

    def row_by_name(self, name: str) -> DutyRow | None:
        k = norm_name(name)
        return next((r for r in self.rows if r.key == k), None)


def parse_duty_grid(
    values: list[list[Any]], cfg: DutySheetCfg, title: str, sheet_id: int, today: date | None = None
) -> DutyTab:
    def row(n: int) -> list[Any]:
        return values[n - 1] if 0 < n <= len(values) else []

    day_row, slot_row = row(cfg.day_header_row), row(cfg.slot_header_row)
    slot_labels = {norm_header(x) for x in cfg.slot_labels}
    slots: list[Slot] = []
    current_day: int | None = None
    width = max(len(day_row), len(slot_row))
    count_col = worklog_col = None
    for c in range(width):
        d = _day_index(cell_str(day_row[c])) if c < len(day_row) else None
        head = norm_header(day_row[c]) if c < len(day_row) else ""
        if d is not None:
            current_day = d
        elif head:
            current_day = None  # gặp tiêu đề khác (nội dung công việc...) → hết vùng buổi trực
        if head == norm_header(cfg.count_header):
            count_col = c + 1
        if head == norm_header(cfg.worklog_header):
            worklog_col = c + 1
        lbl = cell_str(slot_row[c]) if c < len(slot_row) else ""
        if current_day is not None and norm_header(lbl) in slot_labels:
            slots.append(Slot(c + 1, current_day, lbl))
    if not slots:
        raise HeaderMissing(
            f"Tab lịch trực '{title}': không dò được cột buổi trực ở dòng "
            f"{cfg.day_header_row}–{cfg.slot_header_row}"
        )
    no_c, name_c = col_index(cfg.no_col), col_index(cfg.name_col)
    rows: list[DutyRow] = []
    numbered: list[int] = []
    for rn in range(cfg.first_member_row, len(values) + 1):
        r = row(rn)
        no = cell_str(r[no_c - 1]) if len(r) >= no_c else ""
        name = cell_str(r[name_c - 1]) if len(r) >= name_c else ""
        if not _is_number(no):
            continue
        numbered.append(rn)
        if not name:
            continue
        marks = {s.col: cell_str(r[s.col - 1]) for s in slots if len(r) >= s.col and cell_str(r[s.col - 1])}
        rows.append(DutyRow(rn, no, name, marks))
    rng = parse_tab_name(title, today) if today else None
    return DutyTab(
        title,
        sheet_id,
        rng[0] if rng else None,
        rng[1] if rng else None,
        slots,
        rows,
        count_col,
        worklog_col,
        numbered,
    )


# ------------------------------------------------------------------ giao buổi trực T


@dataclass
class MemberLoad:
    name: str
    registered: int  # số buổi đăng ký (V + T)
    target: int  # chỉ tiêu T theo số buổi đăng ký
    t: int  # số T sau khi giao


@dataclass
class DutyPlan:
    """Kết quả giao T: các ô V được đổi thành T + những gì không giao được (cần leader xử lý)."""

    assigned: list[tuple[DutyRow, Slot]] = field(default_factory=list)
    uncovered: list[Slot] = field(default_factory=list)  # buổi không ai đăng ký V/T
    no_slot: list[str] = field(default_factory=list)  # người chưa đăng ký buổi nào → không giao được T
    loads: list[MemberLoad] = field(default_factory=list)


def t_target(registered: int, per_t: int) -> int:
    """Chỉ tiêu T = số buổi đăng ký / per_t, làm tròn (≥ .5 lên), tối thiểu 1. per_t=3: 3→1, 5,6→2, 8→3."""
    if registered <= 0:
        return 0
    return max(1, (2 * registered + per_t) // (2 * per_t))


def plan_duty(tab: DutyTab, available: list[str], fixed: str, per_t: int = 3) -> DutyPlan:
    """Đổi một số ô V thành T. Quy tắc (theo thứ tự ưu tiên):

    1. Mỗi buổi có ≥1 T (nếu có người đăng ký buổi đó).
    2. Mỗi thành viên có ≥1 T, và đạt chỉ tiêu `t_target(số buổi đăng ký)`: đăng ký nhiều thì trực nhiều.
    3. Khi phải giao vượt chỉ tiêu để phủ buổi, chia theo tỉ lệ T/chỉ tiêu → người đăng ký nhiều nhận phần dư.

    - Ô T có sẵn (leader tự giao, hoặc lần chạy trước) được giữ nguyên và tính vào chỉ tiêu.
    - V/T không phân biệt hoa thường. Chỉ đổi ô V, không bao giờ xoá T có sẵn.
    - Buổi ít người đăng ký được xếp trước; người được giao T thêm sẽ nhận buổi đang ít T nhất.
    - Cuối cùng bỏ bớt T vừa giao nếu thừa (buổi vẫn còn T khác và người đó vẫn vượt chỉ tiêu).
    Hàm sửa `tab.rows[*].marks` tại chỗ để báo cáo hiển thị đúng lịch sau khi giao.
    """
    avail = {m.casefold() for m in available} - {fixed.casefold()}
    fx = fixed.casefold()

    def mark(r: DutyRow, s: Slot) -> str:
        return cell_str(r.marks.get(s.col)).casefold()

    t_of = {id(r): sum(1 for s in tab.slots if mark(r, s) == fx) for r in tab.rows}
    t_in = {s.col: sum(1 for r in tab.rows if mark(r, s) == fx) for s in tab.slots}
    registered = {
        id(r): sum(1 for s in tab.slots if mark(r, s) in avail or mark(r, s) == fx) for r in tab.rows
    }
    target = {k: t_target(v, per_t) for k, v in registered.items()}
    plan = DutyPlan()
    new: list[tuple[DutyRow, Slot, str]] = []  # (dòng, buổi, giá trị gốc của ô)

    def prio(r: DutyRow) -> tuple[bool, float, int, int]:
        """Ai chưa có T trước (người ít buổi đăng ký trước, vì khó xếp chỗ khác); sau đó chọn người mà nhận
        thêm 1 T thì tỉ lệ T/chỉ tiêu vẫn thấp nhất → phần T dư rơi vào người đăng ký nhiều hơn."""
        t = t_of[id(r)]
        return t > 0, (t + 1) / max(1, target[id(r)]), registered[id(r)], r.row_number

    def give(r: DutyRow, s: Slot) -> None:
        new.append((r, s, cell_str(r.marks.get(s.col))))
        r.marks[s.col] = fixed
        t_of[id(r)] += 1
        t_in[s.col] += 1

    # 1) mỗi buổi ≥ 1 T — buổi ít lựa chọn nhất xếp trước; chọn người đang thấp nhất so với chỉ tiêu
    while True:
        open_slots = [
            (s, [r for r in tab.rows if mark(r, s) in avail]) for s in tab.slots if t_in[s.col] == 0
        ]
        open_slots = [(s, rs) for s, rs in open_slots if rs]
        if not open_slots:
            break
        s, rs = min(open_slots, key=lambda x: (len(x[1]), x[0].col))
        give(s=s, r=min(rs, key=prio))

    # 2) mỗi người đạt chỉ tiêu (≥1) — người thấp nhất so với chỉ tiêu nhận trước, vào buổi đang ít T nhất
    plan.no_slot = [r.name for r in tab.rows if registered[id(r)] == 0]
    while True:
        need = [
            r for r in tab.rows if t_of[id(r)] < target[id(r)] and any(mark(r, s) in avail for s in tab.slots)
        ]
        if not need:
            break
        r = min(need, key=prio)
        mine = [s for s in tab.slots if mark(r, s) in avail]
        give(r, min(mine, key=lambda s: (t_in[s.col], s.col)))

    # 3) bỏ T thừa vừa giao (người vượt chỉ tiêu nhiều nhất trước)
    for r, s, old in sorted(new, key=lambda x: (-(t_of[id(x[0])] - target[id(x[0])]), x[1].col)):
        if t_in[s.col] > 1 and t_of[id(r)] > max(1, target[id(r)]):
            r.marks[s.col] = old
            t_of[id(r)] -= 1
            t_in[s.col] -= 1
        else:
            plan.assigned.append((r, s))

    plan.assigned.sort(key=lambda x: (x[1].col, x[0].row_number))
    plan.uncovered = [s for s in tab.slots if t_in[s.col] == 0]
    plan.loads = [MemberLoad(r.name, registered[id(r)], target[id(r)], t_of[id(r)]) for r in tab.rows]
    return plan


def tab_link(spreadsheet_id: str, sheet_id: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}"


@dataclass
class CreateResult:
    created: bool
    tab: str
    sheet_id: int
    members_written: int = 0
    fixed_copied: int = 0
    rows_inserted: int = 0
    skipped_names: list[str] = field(default_factory=list)


class DutyRepo:
    def __init__(self, sheets: SheetsAPI, paths: PathResolver, cfg: DutySheetCfg) -> None:
        self.sheets = sheets
        self.paths = paths
        self.cfg = cfg

    async def spreadsheet_id(self) -> str:
        return await self.paths.file_id(self.cfg.path)

    def name_for(self, start: date, end: date) -> str:
        return tab_name(start, end, self.cfg.tab_name_format)

    async def tabs(self) -> list[SheetProps]:
        return await self.sheets.get_tabs(await self.spreadsheet_id())

    async def find_tab(self, start: date, end: date) -> SheetProps | None:
        name = self.name_for(start, end)
        return next((t for t in await self.tabs() if t.title.strip() == name), None)

    async def load_tab(self, props: SheetProps, today: date | None = None) -> DutyTab:
        sid = await self.spreadsheet_id()
        values = await self.sheets.get_values(sid, f"{quote_tab(props.title)}!A1:AZ300")
        return parse_duty_grid(values, self.cfg, props.title, props.sheet_id, today)

    async def load_week(self, start: date, end: date) -> DutyTab | None:
        props = await self.find_tab(start, end)
        return await self.load_tab(props, start) if props else None

    async def write_marks(self, tab: DutyTab, cells: list[tuple[DutyRow, Slot]], value: str) -> None:
        sid = await self.spreadsheet_id()
        await self.sheets.batch_update_values(
            sid,
            [(f"{quote_tab(tab.title)}!{col_letter(s.col)}{r.row_number}", [[value]]) for r, s in cells],
        )

    async def create_week_tab(
        self, start: date, end: date, member_names: list[str], prev: DutyTab | None, *, dry_run: bool = False
    ) -> CreateResult:
        sid = await self.spreadsheet_id()
        name = self.name_for(start, end)
        tabs = await self.sheets.get_tabs(sid)
        existing = next((t for t in tabs if t.title.strip() == name), None)
        if existing:
            return CreateResult(False, name, existing.sheet_id)
        if dry_run:
            return CreateResult(True, name, -1, members_written=len(member_names))

        # 1) copy tab template sang DutySchedule (tái sử dụng bản copy dở dang nếu lần trước lỗi giữa chừng)
        tpl_sid = await self.paths.file_id(self.cfg.template_path)
        tpl_tabs = await self.sheets.get_tabs(tpl_sid)
        tpl = (
            next((t for t in tpl_tabs if t.title == self.cfg.template_tab), None)
            if self.cfg.template_tab
            else (tpl_tabs[0] if tpl_tabs else None)
        )
        if tpl is None:
            raise HeaderMissing(f"Không tìm thấy tab template trong {self.cfg.template_path}")
        leftover = next(
            (t for t in tabs if t.title in {f"Copy of {tpl.title}", f"Bản sao của {tpl.title}"}), None
        )
        new = leftover or await self.sheets.copy_sheet_to(tpl_sid, tpl.sheet_id, sid)
        await self.sheets.batch_update(
            sid,
            [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": new.sheet_id, "title": name},
                        "fields": "title",
                    }
                }
            ],
        )

        # 2) đọc bố cục tab mới
        values = await self.sheets.get_values(sid, f"{quote_tab(name)}!A1:AZ300")
        tab = parse_duty_grid(values, self.cfg, name, new.sheet_id, start)
        numbered = tab.numbered_rows or [self.cfg.first_member_row]
        first = numbered[0]
        n = len(member_names)
        result = CreateResult(True, name, new.sheet_id, members_written=n)

        # 3) chèn thêm dòng nếu nhiều thành viên hơn số dòng đánh số sẵn
        extra = n - len(numbered)
        if extra > 0:
            last = numbered[-1]
            await self.sheets.batch_update(
                sid,
                [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": new.sheet_id,
                                "dimension": "ROWS",
                                "startIndex": last,
                                "endIndex": last + extra,
                            },
                            "inheritFromBefore": True,
                        }
                    }
                ],
            )
            result.rows_inserted = extra

        no_c, name_c = col_index(self.cfg.no_col), col_index(self.cfg.name_col)
        title_col, title_row = parse_cell(self.cfg.title_cell)
        data: list[tuple[str, list[list[Any]]]] = [
            (
                f"{quote_tab(name)}!{col_letter(title_col)}{title_row}",
                [[self.cfg.title_format.format(start=start, end=end)]],
            ),
            (
                f"{quote_tab(name)}!{col_letter(name_c)}{first}:{col_letter(name_c)}{first + n - 1}",
                [[nm] for nm in member_names],
            ),
        ]
        if extra > 0:
            data.append(
                (
                    f"{quote_tab(name)}!{col_letter(no_c)}{first}:{col_letter(no_c)}{first + n - 1}",
                    [[i + 1] for i in range(n)],
                )
            )

        # 4) chép ô T (lịch cố định) từ tuần hiện tại, ghép theo họ tên và theo (thứ, buổi)
        if prev is not None:
            slot_by_key = {s.key: s for s in tab.slots}
            for i, nm in enumerate(member_names):
                old = prev.row_by_name(nm)
                if old is None:
                    continue
                for s in prev.fixed_marks(old, self.cfg.fixed_mark):
                    target = slot_by_key.get(s.key)
                    if target:
                        data.append(
                            (
                                f"{quote_tab(name)}!{col_letter(target.col)}{first + i}",
                                [[self.cfg.fixed_mark]],
                            )
                        )
                        result.fixed_copied += 1
            current = {norm_name(x) for x in member_names}
            result.skipped_names = [
                r.name for r in prev.rows if r.key not in current and prev.fixed_marks(r, self.cfg.fixed_mark)
            ]
        await self.sheets.batch_update_values(sid, data)

        # 5) khoá cột No, Name
        await self.sheets.batch_update(
            sid,
            [
                {
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {
                                "sheetId": new.sheet_id,
                                "startColumnIndex": min(no_c, name_c) - 1,
                                "endColumnIndex": max(no_c, name_c),
                            },
                            "description": f"labagent: khoá cột No/Name tab {name}",
                            "warningOnly": False,
                        }
                    }
                }
            ],
        )
        return result
