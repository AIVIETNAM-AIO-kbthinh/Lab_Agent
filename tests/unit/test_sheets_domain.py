from datetime import date, timedelta

import pytest

from app.auth.roles import Role, resolve_role
from app.config import MembersSheetCfg
from app.domain.duty import DutyRepo, parse_duty_grid
from app.domain.equipment import EquipmentRepo
from app.domain.members import MemberDirectory
from app.errors import DuplicateNames, HeaderMissing
from app.google.paths import PathResolver
from app.sheets.table import SheetTable
from tests.conftest import TODAY, make_loaded
from tests.factories import LabFixture, duty_grid, marks
from tests.fakes.sheets import FakeSheets


@pytest.fixture
def lab() -> LabFixture:
    return LabFixture(TODAY)


@pytest.fixture
def cfg():  # type: ignore[no-untyped-def]
    return make_loaded().config


async def test_sheet_table_by_header_and_reordered_columns() -> None:
    s = FakeSheets()
    s.add_tab(
        "X", "T", [["Ghi chú", "  mã   THIẾT bị ", "Thiết bị", "Bí mật"], ["n1", "TB1", "Máy A", "secret"]]
    )
    t = await SheetTable(
        s, "X", "T", {"ma_tb": "Mã thiết bị", "ten_tb": "Thiết bị", "ghi_chu": "Ghi chú"}
    ).load()
    r = t.rows()[0]
    assert r.values == {
        "ma_tb": "TB1",
        "ten_tb": "Máy A",
        "ghi_chu": "n1",
    }  # cột "Bí mật" không bao giờ đọc vào
    await t.update(r.row_number, {"ghi_chu": "n2"})
    assert s.values("X", "T")[1] == ["n2", "TB1", "Máy A", "secret"]
    rn = await t.append({"ma_tb": "TB2", "ten_tb": "Máy B"})
    assert rn == 3 and s.values("X", "T")[2][:3] == ["", "TB2", "Máy B"]


async def test_sheet_table_missing_header_raises() -> None:
    s = FakeSheets()
    s.add_tab("X", "T", [["A", "B"]])
    with pytest.raises(HeaderMissing):
        await SheetTable(s, "X", "T", {"a": "A", "c": "C"}).load()
    t = await SheetTable(s, "X", "T", {"a": "A", "c": "C"}, optional=["c"]).load()
    assert t.missing_optional == ["C"]


async def test_members_and_roles(lab: LabFixture, cfg) -> None:  # type: ignore[no-untyped-def]
    d = MemberDirectory(lab.sheets, PathResolver(lab.drive, "ROOT"), cfg.sheets.members)
    snap = await d.snapshot()
    assert [m.ho_ten for m in snap.active] == [
        "Nguyễn Văn Leader",
        "Trần Thị Thư",
        "Lê Văn An",
        "Phạm Thị Bình",
    ]
    assert snap.by_email("BINH@vnu.edu.vn").sdt == "0944444444"  # type: ignore[union-attr]
    assert snap.leader().ho_ten == "Nguyễn Văn Leader"  # type: ignore[union-attr]
    assert snap.former_by_name("Đỗ Văn Cũ") is not None
    admins = ["admin@gmail.com"]
    assert resolve_role("leader@gmail.com", snap, admins)[0] == Role.LEADER
    assert resolve_role("thu@gmail.com", snap, admins)[0] == Role.THU_KI
    assert resolve_role("an@gmail.com", snap, admins)[0] == Role.MEMBER
    assert resolve_role("Admin@Gmail.com", snap, admins)[0] == Role.ADMIN
    assert resolve_role("cu@gmail.com", snap, admins)[0] == Role.GUEST  # cựu thành viên ⇒ guest
    assert resolve_role(None, snap, admins)[0] == Role.GUEST


async def test_duplicate_names_detected(lab: LabFixture, cfg) -> None:  # type: ignore[no-untyped-def]
    lab.sheets.values(lab.members_id, "Members").append(
        [5, "SV5", "lê  văn an", "", "", "", "", "an2@gmail.com", "Member"]
    )
    snap = await MemberDirectory(lab.sheets, PathResolver(lab.drive, "ROOT"), cfg.sheets.members).snapshot()
    with pytest.raises(DuplicateNames):
        snap.assert_unique_names()


async def test_equipment_availability_and_overdue(lab: LabFixture, cfg) -> None:  # type: ignore[no-untyped-def]
    repo = EquipmentRepo(lab.sheets, PathResolver(lab.drive, "ROOT"), cfg.sheets.equipment)
    snap = await repo.snapshot()
    av = snap.availability("TB002")
    assert av is not None and (av.dang_muon, av.con_lai) == (4, 6)
    assert snap.availability("TB001").con_lai == 1  # type: ignore[union-attr]
    assert snap.availability("TB003").con_lai == 0  # type: ignore[union-attr]  # Hỏng ⇒ không cho mượn
    assert [b.ho_ten for b in snap.overdue(TODAY)] == ["Phạm Thị Bình"]
    assert [b.ho_ten for b in snap.due_on(TODAY + timedelta(days=1))] == ["Lê Văn An"]
    old = next(b for b in snap.borrows if not b.ma_tb)
    assert snap.resolved_ma_tb(old) == "TB001"  # dòng cũ khớp theo tên thiết bị
    assert [d.ma_tb for d in snap.search("may chieu")] == ["TB001"]
    assert "Lê Văn An - 1 -" in snap.render_nguoi_muon_cell("TB001")


async def test_equipment_writes_idempotent(lab: LabFixture, cfg) -> None:  # type: ignore[no-untyped-def]
    repo = EquipmentRepo(lab.sheets, PathResolver(lab.drive, "ROOT"), cfg.sheets.equipment)
    row = {
        "ngay_muon": TODAY,
        "ho_ten": "Lê Văn An",
        "ten_tb": "Arduino Uno",
        "ma_tb": "TB002",
        "so_luong": 2,
        "ma_phieu": "P-20261016-01",
        "han_tra": TODAY + timedelta(days=7),
    }
    assert len(await repo.append_borrow_rows([row])) == 1
    assert await repo.append_borrow_rows([row]) == []  # chạy lại không tạo trùng
    snap = await repo.snapshot()
    b = next(x for x in snap.borrows if x.ma_phieu == "P-20261016-01")
    await repo.split_partial_return(b, 1, TODAY, "Tốt")
    snap = await repo.snapshot()
    rows = [x for x in snap.borrows if x.ma_phieu == "P-20261016-01"]
    assert sorted((x.so_luong, x.active) for x in rows) == [(1, False), (1, True)]
    await repo.refresh_nguoi_muon(["TB002"])
    cell = lab.sheets.values(lab.equipment_id, "Thietbi")[2][6]
    assert "Phạm Thị Bình - 3" in cell and "Lê Văn An - 1" in cell


def test_duty_grid_parsing(cfg) -> None:  # type: ignore[no-untyped-def]
    grid = duty_grid("t", [("A", marks(t2s="v", t2c="T", cnc="V")), ("B", [])], numbered=4)
    tab = parse_duty_grid(grid, cfg.sheets.duty, "12/10-18/10", 1, date(2026, 10, 16))
    assert len(tab.slots) == 14 and tab.slots[0].col == 3 and tab.slots[-1].day == 6
    assert tab.count_col == 19 and tab.worklog_col == 17
    assert [r.name for r in tab.rows] == ["A", "B"] and tab.numbered_rows == [7, 8, 9, 10]
    assert tab.count_sessions(tab.rows[0], ["V", "T"]) == 3
    assert [s.label for s in tab.fixed_marks(tab.rows[0], "T")] == ["Chiều"]
    assert tab.start == date(2026, 10, 12)


async def test_create_week_tab(lab: LabFixture, cfg) -> None:  # type: ignore[no-untyped-def]
    repo = DutyRepo(lab.sheets, PathResolver(lab.drive, "ROOT"), cfg.sheets.duty)
    cur = await repo.load_week(date(2026, 10, 12), date(2026, 10, 18))
    names = ["Nguyễn Văn Leader", "Trần Thị Thư", "Lê Văn An", "Phạm Thị Bình"]
    res = await repo.create_week_tab(date(2026, 10, 19), date(2026, 10, 25), names, cur)
    assert res.created and res.tab == "19/10-25/10" and res.rows_inserted == 1 and res.fixed_copied == 2
    grid = lab.sheets.values(lab.duty_id, "19/10-25/10")
    assert grid[0][0] == "LỊCH TRỰC LAB TUẦN TỪ 19/10 - 25/10"
    assert [r[1] for r in grid[6:10]] == names and [r[0] for r in grid[6:10]] == [1, 2, 3, 4]
    assert grid[5][0] == "Thầy Nguyễn Đăng Khoa"
    new = await repo.load_week(date(2026, 10, 19), date(2026, 10, 25))
    assert new is not None
    leader = new.row_by_name("Nguyễn Văn Leader")
    assert (
        leader is not None and new.count_sessions(leader, ["V", "T"]) == 2
    )  # chỉ ô T được chép, ô v thì không
    assert lab.sheets.protected  # khoá cột No/Name
    again = await repo.create_week_tab(date(2026, 10, 19), date(2026, 10, 25), names, cur)
    assert not again.created  # chạy lại không tạo trùng
    assert sum(1 for t in lab.sheets.books[lab.duty_id] if t.title == "19/10-25/10") == 1


def test_members_cfg_roles(cfg) -> None:  # type: ignore[no-untyped-def]
    assert isinstance(cfg.sheets.members, MembersSheetCfg)
    assert cfg.sheets.members.header_row == 3
