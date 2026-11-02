from datetime import date

import pytest

from app.domain.dates import fmt_date, next_week, parse_sheet_date, parse_tab_name, resolve_time_expr, week_of
from app.domain.names import (
    folder_name,
    fuzzy_match,
    norm_name,
    normalize_phone,
    to_pascal_ascii,
    valid_email,
    valid_phone,
)
from app.sheets.a1 import cell, col_index, col_letter, first_row_of, quote_tab


def test_pascal_and_folder_name() -> None:
    assert to_pascal_ascii("Nguyễn Văn Đức") == "NguyenVanDuc"
    assert to_pascal_ascii("  đỗ   thị  hà ") == "DoThiHa"
    assert folder_name("Nguyễn Văn Đức", "+84912345678") == "NguyenVanDuc_0912345678"


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("+84 912 345 678", "0912345678"),
        ("0912.345.678", "0912345678"),
        ("912345678", "0912345678"),
        ("(091) 234-5678", "0912345678"),
    ],
)
def test_phone(raw: str, norm: str) -> None:
    assert normalize_phone(raw) == norm
    assert valid_phone(raw)


def test_email_domains() -> None:
    assert valid_email("a.b@gmail.com", ["gmail.com", "vnu.edu.vn"])
    assert valid_email("x@vnu.edu.vn", ["gmail.com", "vnu.edu.vn"])
    assert not valid_email("x@yahoo.com", ["gmail.com"])
    assert not valid_email("not-an-email", None)


def test_norm_name_and_fuzzy() -> None:
    assert norm_name("  Nguyễn  Văn  A ") == norm_name("nguyễn văn a")
    names = ["Đào Hoàng Trung", "Nguyễn Tuấn Kiệt", "Phạm Ngọc Tùng"]
    assert fuzzy_match("dao hoang trung", names)[0][0] == "Đào Hoàng Trung"
    assert fuzzy_match("Trung", names)[0][0] == "Đào Hoàng Trung"
    assert fuzzy_match("Nguyễn Tuấn Kiệt", names) == [("Nguyễn Tuấn Kiệt", 100.0)]


def test_parse_sheet_date_serial_and_strings() -> None:
    assert parse_sheet_date(46311) == date(2026, 10, 16)  # serial Google Sheets (epoch 30/12/1899)
    assert parse_sheet_date(46311.75) == date(2026, 10, 16)
    assert parse_sheet_date("15/10/2026") == date(2026, 10, 15)
    assert parse_sheet_date("5/1/26") == date(2026, 1, 5)
    assert parse_sheet_date("2026-10-15") == date(2026, 10, 15)
    assert parse_sheet_date("16/10/2026 10:00") == date(2026, 10, 16)
    assert parse_sheet_date("") is None
    assert parse_sheet_date("31/02/2026") is None
    assert fmt_date(date(2026, 1, 5)) == "05/01/2026"


def test_week_and_tab_names() -> None:
    assert week_of(date(2026, 10, 16)) == (date(2026, 10, 12), date(2026, 10, 18))
    assert next_week(date(2026, 10, 16)) == (date(2026, 10, 19), date(2026, 10, 25))
    assert parse_tab_name("12/10-18/10", date(2026, 10, 16)) == (date(2026, 10, 12), date(2026, 10, 18))
    # tuần vắt năm
    assert parse_tab_name("29/12-04/01", date(2026, 12, 30)) == (date(2026, 12, 29), date(2027, 1, 4))
    assert parse_tab_name("29/12-04/01", date(2027, 1, 2)) == (date(2026, 12, 29), date(2027, 1, 4))
    assert parse_tab_name("Template", date(2026, 1, 1)) is None


def test_resolve_time_expr() -> None:
    today = date(2026, 10, 16)
    assert resolve_time_expr("tuần trước", today) == (date(2026, 10, 5), date(2026, 10, 11))
    assert resolve_time_expr("Tuần này", today) == (date(2026, 10, 12), date(2026, 10, 18))
    assert resolve_time_expr("tuan sau", today) == (date(2026, 10, 19), date(2026, 10, 25))
    assert resolve_time_expr("tháng 9", today) == (date(2026, 9, 1), date(2026, 9, 30))
    assert resolve_time_expr("tháng 12", today) == (date(2025, 12, 1), date(2025, 12, 31))
    assert resolve_time_expr("tháng trước", today) == (date(2026, 9, 1), date(2026, 9, 30))
    assert resolve_time_expr("hôm qua", today) == (date(2026, 10, 15), date(2026, 10, 15))
    assert resolve_time_expr("12/10 - 18/10", today) == (date(2026, 10, 12), date(2026, 10, 18))
    assert resolve_time_expr("20/10", today) == (date(2026, 10, 20), date(2026, 10, 20))
    assert resolve_time_expr("lúc nào đó", today) is None


def test_a1() -> None:
    assert col_letter(1) == "A" and col_letter(27) == "AA" and col_index("AA") == 27
    assert quote_tab("12/10-18/10") == "'12/10-18/10'"
    assert quote_tab("it's") == "'it''s'"
    assert cell("12/10-18/10", 2, 7) == "'12/10-18/10'!B7"
    assert first_row_of("'LichSuMuon'!A12:L13") == 12
