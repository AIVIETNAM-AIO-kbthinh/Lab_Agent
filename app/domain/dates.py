"""Ngày tháng: ô sheet (chuỗi hoặc serial), tuần, tên tab lịch trực, biểu thức thời gian tiếng Việt."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from app.domain.names import ascii_key

SERIAL_EPOCH = date(1899, 12, 30)  # Google Sheets / Excel


def parse_sheet_date(v: Any) -> date | None:
    """Ô ngày có thể là số serial (UNFORMATTED_VALUE) hoặc chuỗi dd/mm/yyyy."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if 1 <= v < 200000:
            return SERIAL_EPOCH + timedelta(days=int(v))
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_sheet_date(float(s))
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def fmt_date(d: date | None, fmt: str = "%d/%m/%Y") -> str:
    return d.strftime(fmt) if d else ""


def week_of(d: date) -> tuple[date, date]:
    """(thứ 2, chủ nhật) của tuần chứa d."""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def next_week(d: date) -> tuple[date, date]:
    s, _ = week_of(d)
    return s + timedelta(days=7), s + timedelta(days=13)


def tab_name(start: date, end: date, fmt: str = "{start:%d/%m}-{end:%d/%m}") -> str:
    return fmt.format(start=start, end=end)


_TAB = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})\s*$")


def parse_tab_name(name: str, today: date) -> tuple[date, date] | None:
    """ "12/10-18/10" -> (start, end); năm suy theo ngày gần `today` nhất."""
    m = _TAB.match(name)
    if not m:
        return None
    d1, m1, d2, m2 = (int(x) for x in m.groups())
    best: tuple[date, date] | None = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            start = date(y, m1, d1)
            end = date(y + (1 if m2 < m1 else 0), m2, d2)
        except ValueError:
            continue
        if best is None or abs((start - today).days) < abs((best[0] - today).days):
            best = (start, end)
    return best


def month_range(y: int, m: int) -> tuple[date, date]:
    start = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def resolve_time_expr(expr: str, today: date) -> tuple[date, date] | None:
    """Chuyển biểu thức thời gian tiếng Việt thành khoảng ngày [start, end]. Không hiểu → None."""
    s = ascii_key(expr)
    if not s:
        return None
    if s in {"hom nay", "today", "nay"}:
        return today, today
    if s in {"hom qua", "yesterday"}:
        d = today - timedelta(days=1)
        return d, d
    if s in {"ngay mai", "mai", "tomorrow"}:
        d = today + timedelta(days=1)
        return d, d
    if s in {"tuan nay", "this week", "current"}:
        return week_of(today)
    if s in {"tuan truoc", "tuan roi", "last week", "previous"}:
        return week_of(today - timedelta(days=7))
    if s in {"tuan sau", "tuan toi", "next week", "next"}:
        return next_week(today)
    if s in {"thang nay", "this month"}:
        return month_range(today.year, today.month)
    if s in {"thang truoc", "thang roi", "last month"}:
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return month_range(y, m)
    if s in {"thang sau", "thang toi", "next month"}:
        y, m = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        return month_range(y, m)
    if s in {"nam nay", "this year"}:
        return date(today.year, 1, 1), date(today.year, 12, 31)
    mm = re.fullmatch(r"thang (\d{1,2})(?:[/ ](?:nam )?(\d{4}))?", s)
    if mm:
        mo = int(mm.group(1))
        if not 1 <= mo <= 12:
            return None
        y = int(mm.group(2)) if mm.group(2) else (today.year if mo <= today.month else today.year - 1)
        return month_range(y, mo)
    rm = re.fullmatch(r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*(?:-|den|toi)\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", s)
    if rm:
        a = _date_with_default_year(rm.group(1), today)
        b = _date_with_default_year(rm.group(2), today)
        if a and b:
            return (a, b) if a <= b else (b, a)
    single = _date_with_default_year(s, today)
    if single:
        return single, single
    return None


def _date_with_default_year(s: str, today: date) -> date | None:
    if re.fullmatch(r"\d{1,2}/\d{1,2}", s):
        s = f"{s}/{today.year}"
    return parse_sheet_date(s)
