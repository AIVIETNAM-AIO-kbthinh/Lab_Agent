"""pick_name: lọc theo người không được đoán sang người khác có tên gần giống."""

import pytest

from app.exports.build import ExportError, pick_name

WITH_DATA = ["Nguyễn Đức Thịnh", "Đào Hoàng Trung"]  # người có dữ liệu (vd đã từng mượn)
KNOWN = ["Nguyễn Minh Thịnh", "Nguyễn Đức Thịnh", "Đào Hoàng Trung", "Kiều Bá Thịnh"]  # Members + admin


def test_real_person_without_data_is_not_mapped_to_someone_else() -> None:
    # lỗi cũ: chỉ so với người có dữ liệu ⇒ "Nguyễn Minh Thịnh" bị hiểu thành "Nguyễn Đức Thịnh"
    assert pick_name("Nguyễn Minh Thịnh", WITH_DATA, "người mượn", known=KNOWN) == "Nguyễn Minh Thịnh"


def test_exact_and_partial_match_still_work() -> None:
    notes: list[str] = []
    assert (
        pick_name("nguyen duc thinh", WITH_DATA, "người mượn", known=KNOWN, notes=notes) == "Nguyễn Đức Thịnh"
    )
    assert notes == []
    assert pick_name("Trung", WITH_DATA, "người mượn", known=KNOWN, notes=notes) == "Đào Hoàng Trung"
    assert notes and "Đào Hoàng Trung" in notes[0]  # không khớp nguyên văn ⇒ ghi chú đã hiểu là ai


def test_ambiguous_name_asks_back() -> None:
    with pytest.raises(ExportError) as e:
        pick_name("Thịnh", WITH_DATA, "người mượn", known=KNOWN)
    assert "gần giống" in str(e.value)
