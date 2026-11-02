"""Tiện ích ký hiệu A1. Tên tab luôn bọc nháy đơn: '12/10-18/10'!A1."""

from __future__ import annotations

import re


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    if n < 1:
        raise ValueError(n)
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    """A -> 1, AA -> 27."""
    n = 0
    for ch in letters.strip().upper():
        if not "A" <= ch <= "Z":
            raise ValueError(letters)
        n = n * 26 + (ord(ch) - 64)
    return n


def quote_tab(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def cell(tab: str, col: int, row: int) -> str:
    return f"{quote_tab(tab)}!{col_letter(col)}{row}"


def rng(tab: str, a1: str) -> str:
    return f"{quote_tab(tab)}!{a1}"


_CELL = re.compile(r"^([A-Z]+)(\d+)$")


def parse_cell(a1: str) -> tuple[int, int]:
    """'B7' -> (col=2, row=7)."""
    m = _CELL.match(a1.strip().upper())
    if not m:
        raise ValueError(a1)
    return col_index(m.group(1)), int(m.group(2))


_UPDATED = re.compile(r"!([A-Z]+)(\d+)")


def first_row_of(updated_range: str) -> int | None:
    """ "'Tab'!A12:L12" -> 12."""
    m = _UPDATED.search(updated_range)
    return int(m.group(2)) if m else None
