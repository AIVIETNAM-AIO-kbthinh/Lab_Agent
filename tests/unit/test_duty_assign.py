"""Giao buổi trực T tự động (plan_duty): mỗi buổi ≥1 T, mỗi thành viên ≥1 T và đạt chỉ tiêu theo số buổi
đăng ký (nhiều buổi → nhiều T); chỉ đổi ô V, giữ T có sẵn."""

from __future__ import annotations

import random
from datetime import date
from typing import Any

import pytest

from app.config import DutySheetCfg
from app.domain.duty import DutyTab, parse_duty_grid, plan_duty, t_target
from tests.conftest import make_loaded
from tests.factories import duty_grid, marks

KEYS = [f"{d}{s}" for d in ("t2", "t3", "t4", "t5", "t6", "t7", "cn") for s in ("s", "c")]


@pytest.fixture
def dcfg() -> DutySheetCfg:
    return make_loaded().config.sheets.duty


def _tab(dcfg: DutySheetCfg, people: list[tuple[str, list[str]]]) -> DutyTab:
    return parse_duty_grid(
        duty_grid("t", people, numbered=len(people)), dcfg, "19/10-25/10", 1, date(2026, 10, 16)
    )


def _t_slots(tab: DutyTab, name: str) -> list[str]:
    row = tab.row_by_name(name)
    assert row is not None
    return [tab.slot_text(s) for s in tab.fixed_marks(row, "T")]


def _plan(tab: DutyTab) -> Any:
    return plan_duty(tab, ["V", "T"], "T")


def test_each_slot_and_member_get_t(dcfg: DutySheetCfg) -> None:
    tab = _tab(
        dcfg,
        [
            ("A", marks(t2s="V", t2c="v", t3s="V")),  # chữ thường cũng tính
            ("B", marks(t2s="v")),  # chỉ rảnh 1 buổi → phải được giao đúng buổi đó
            ("C", marks(t3s="V", t2c="V")),
            ("D", []),  # chưa đăng ký buổi nào
        ],
    )
    plan = _plan(tab)
    assert _t_slots(tab, "B") == ["Thứ 2 (19/10) Sáng"]
    for name in ("A", "C"):
        assert len(_t_slots(tab, name)) >= 1
    covered = {s.col for s in tab.slots for r in tab.rows if r.marks.get(s.col) == "T"}
    assert {3, 4, 5} <= covered  # T2 sáng, T2 chiều, T3 sáng
    assert plan.no_slot == ["D"]
    assert len(plan.uncovered) == 11  # 14 buổi − 3 buổi có người đăng ký
    assert len(plan.assigned) == 3  # vừa đủ: không ai bị giao thừa


def test_keeps_existing_t_and_rerun_is_noop(dcfg: DutySheetCfg) -> None:
    tab = _tab(dcfg, [("A", marks(t2s="T", t2c="V")), ("B", marks(t2s="V", t2c="V"))])
    plan = _plan(tab)
    assert _t_slots(tab, "A") == ["Thứ 2 (19/10) Sáng"]  # T có sẵn giữ nguyên, không giao thêm
    assert _t_slots(tab, "B") == ["Thứ 2 (19/10) Chiều"]
    assert [(r.name, s.col) for r, s in plan.assigned] == [("B", 4)]
    again = _plan(tab)
    assert again.assigned == [] and again.uncovered[0].col == 5


def test_target_scales_with_registrations() -> None:
    assert [t_target(n, 3) for n in range(10)] == [0, 1, 1, 1, 1, 2, 2, 2, 3, 3]
    assert t_target(4, 2) == 2 and t_target(1, 5) == 1


def test_more_registrations_more_t(dcfg: DutySheetCfg) -> None:
    """4 người cùng rảnh 6 buổi nhưng đăng ký 3 / 5 / 6 / 9 buổi → chỉ tiêu 1 / 2 / 2 / 3."""
    six = ["t2s", "t2c", "t3s", "t3c", "t4s", "t4c"]
    tab = _tab(
        dcfg,
        [
            ("Ba", marks(**dict.fromkeys(six[:3], "V"))),
            ("Năm", marks(**dict.fromkeys(six[:5], "v"))),
            ("Sáu", marks(**dict.fromkeys(six, "V"))),
            ("Chín", marks(**dict.fromkeys([*six, "t5s", "t5c", "t6s"], "V"))),
        ],
    )
    plan = _plan(tab)
    loads = {x.name: (x.registered, x.target, x.t) for x in plan.loads}
    assert loads == {
        "Ba": (3, 1, 1),
        "Năm": (5, 2, 2),
        "Sáu": (6, 2, 2),
        "Chín": (9, 3, 4),
    }  # 9 buổi cần phủ > tổng chỉ tiêu 8: phần dư cho người đăng ký nhiều nhất
    assert plan.uncovered == [s for s in tab.slots if s.day >= 4 and s.col != 11]  # T6 chiều → CN không ai


def test_coverage_extra_goes_to_who_registered_more(dcfg: DutySheetCfg) -> None:
    """Buổi cần phủ vượt chỉ tiêu được chia theo tỉ lệ T/chỉ tiêu: rảnh 14 buổi nhận nhiều hơn rảnh 4 buổi."""
    tab = _tab(
        dcfg,
        [
            ("Rảnh", marks(**dict.fromkeys(KEYS, "V"))),
            ("B", marks(t2s="V", t3s="V", t4s="V", t5s="V")),
            ("C", marks(t2c="V", t3c="V", t4c="V", t5c="V")),
        ],
    )
    plan = _plan(tab)
    assert {x.name: x.t for x in plan.loads} == {"Rảnh": 10, "B": 2, "C": 2}
    assert plan.uncovered == []


def test_random_schedules_hold_rules(dcfg: DutySheetCfg) -> None:
    rnd = random.Random(7)
    for _ in range(300):
        people = []
        for i in range(rnd.randint(1, 12)):
            cells = {k: rnd.choice(["V", "v"]) for k in KEYS if rnd.random() < rnd.choice([0.05, 0.2, 0.5])}
            if rnd.random() < 0.1 and cells:
                cells[rnd.choice(list(cells))] = "T"
            people.append((f"P{i}", marks(**cells)))
        tab = _tab(dcfg, people)
        before = {(r.name, c): v for r in tab.rows for c, v in r.marks.items()}
        plan = _plan(tab)
        after = {(r.name, c): v for r in tab.rows for c, v in r.marks.items()}
        assert before.keys() == after.keys()  # không thêm ô mới
        for k, v in before.items():
            if v == "T":
                assert after[k] == "T"  # T có sẵn không bị xoá
            elif after[k] != v:
                assert after[k] == "T"  # chỉ đổi V/v → T
        loads = {x.name: x for x in plan.loads}
        for r in tab.rows:
            if r.marks:
                assert tab.fixed_marks(r, "T"), r.name
                x = loads[r.name]
                assert x.t == len(tab.fixed_marks(r, "T")) >= x.target == t_target(len(r.marks), 3)
            else:
                assert r.name in plan.no_slot
        for s in tab.slots:
            has_any = any(r.marks.get(s.col) for r in tab.rows)
            has_t = any(r.marks.get(s.col) == "T" for r in tab.rows)
            assert has_t == has_any and (s in plan.uncovered) == (not has_any)
