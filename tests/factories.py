"""Dữ liệu mẫu ẩn danh theo đúng bố cục sheet của lab (TK §3)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.domain.dates import fmt_date, next_week, week_of
from app.google.drive import DOCX, FOLDER, GSHEET, DriveFile
from tests.fakes.drive import FakeDrive
from tests.fakes.sheets import FakeSheets

MEMBERS_HEADER = ["No", "Student ID", "Name", "Lớp", "Phone", "Address", "University", "Email", "Role"]
MEMBERS = [
    [
        1,
        "SV001",
        "Nguyễn Văn Leader",
        "K66",
        "0911111111",
        "12 Phố Bí Mật",
        "UET",
        "leader@gmail.com",
        "Leader",
    ],
    [2, "SV002", "Trần Thị Thư", "K67", "0922222222", "34 Ngõ Kín", "UET", "thu@gmail.com", "Secretary"],
    [3, "SV003", "Lê Văn An", "K67", "0933333333", "56 Đường Ẩn", "IS", "an@gmail.com", "Member"],
    [4, "SV004", "Phạm Thị Bình", "K68", "+84944444444", "78 Hẻm Riêng", "HUST", "binh@vnu.edu.vn", "Member"],
]
DEVICES_HEADER = [
    "Mã thiết bị",
    "Thiết bị",
    "Trạng thái hoạt động",
    "Số lượng",
    "Ngày kiểm tra",
    "Ghi chú",
    "Người mượn",
    "Còn lại",
]
BORROWS_HEADER = [
    "Thời gian",
    "Tên người mượn",
    "thiết bị mượn",
    "trạng thái",
    "Hạn trả",
    "ngày trả",
    "trạng thái sau trả",
    "Ghi chú",
    "Mã thiết bị",
    "Số lượng",
    "Mã phiếu",
    "Link phiếu",
]
DAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]


def duty_grid(title: str, members: list[tuple[str, list[str]]], numbered: int) -> list[list[Any]]:
    """Bố cục lab: dòng 4 thứ (mỗi thứ 2 cột), dòng 5 Sáng/Chiều, dòng 6 thầy, từ dòng 7 thành viên.

    `members`: (họ tên, 14 ô đánh dấu). Cột C..P buổi trực, Q-R nội dung công việc, S số buổi.
    """
    row4 = ["", ""]
    for d in DAYS:
        row4 += [d, ""]
    row4 += ["Nội dung công việc đang thực hiện", "", "Số buổi trực lab"]
    row5 = ["No", "Name"] + ["Sáng", "Chiều"] * 7
    grid: list[list[Any]] = [
        [title],
        ["Tick v là đăng ký trực, T là lab giao trực"],
        ["Hạn ghi nội dung công việc: trước 0h ngày chủ nhật"],
        row4,
        row5,
        ["Thầy Nguyễn Đăng Khoa"],
    ]
    for i in range(max(numbered, len(members))):
        if i < len(members):
            name, marks = members[i]
            grid.append([i + 1, name, *marks])
        elif i < numbered:
            grid.append([i + 1, ""])
    return grid


def marks(**slots: str) -> list[str]:
    """marks(t2s="v", t2c="T", ...) → 14 ô (t2s = thứ 2 sáng, cns = chủ nhật sáng)."""
    keys = [f"{d}{s}" for d in ("t2", "t3", "t4", "t5", "t6", "t7", "cn") for s in ("s", "c")]
    return [slots.get(k, "") for k in keys]


class LabFixture:
    """Dựng Drive + Sheets giả cho một lab."""

    def __init__(self, today: date) -> None:
        self.today = today
        self.drive = FakeDrive("ROOT")
        self.sheets = FakeSheets()
        d = self.drive
        self.members_id = d.add("data/Members", GSHEET, file_id="S_MEMBERS").id
        self.equipment_id = d.add("data/Equipment", GSHEET, file_id="S_EQUIP").id
        self.duty_id = d.add("data/DutySchedule", GSHEET, file_id="S_DUTY").id
        self.forms_id = d.add("data/FormSubmissions", GSHEET, file_id="S_FORMS").id
        self.audit_id = d.add("data/AuditLog", GSHEET, file_id="S_AUDIT").id
        self.tpl_id = d.add("templates/LichTrucTuan", GSHEET, file_id="S_TPL").id
        for folder in ("kb/public", "kb/internal/meeting-minutes"):
            parts = folder.split("/")
            parent = "ROOT"
            for p in parts:
                f = next((x for x in d.files.values() if x.name == p and parent in x.parents), None)
                if f is None:
                    f = DriveFile(f"D_{p}", p, FOLDER, [parent])
                    d.files[f.id] = f
                parent = f.id

        s = self.sheets
        s.add_tab(self.members_id, "Members", [["INFORMATION LAB MEMBERS"], [], MEMBERS_HEADER, *MEMBERS])
        s.add_tab(
            self.members_id,
            "Cựu thành viên",
            [
                ["CỰU THÀNH VIÊN"],
                [],
                [*MEMBERS_HEADER, "Ngày rời"],
                [
                    9,
                    "SV009",
                    "Đỗ Văn Cũ",
                    "K60",
                    "0999999999",
                    "",
                    "UET",
                    "cu@gmail.com",
                    "Member",
                    "01/01/2026",
                ],
            ],
        )
        t = today
        s.add_tab(
            self.equipment_id,
            "Thietbi",
            [
                DEVICES_HEADER,
                ["TB001", "Máy chiếu Epson", "Tốt", 2, "", "", "", 2],
                ["TB002", "Arduino Uno", "Tốt", 10, "", "", "", 10],
                ["TB003", "Máy in 3D", "Hỏng", 1, "", "đang sửa", "", 1],
            ],
        )
        s.add_tab(
            self.equipment_id,
            "LichSuMuon",
            [
                BORROWS_HEADER,
                # An mượn 1 máy chiếu, hạn ngày mai
                [
                    fmt_date(t - timedelta(days=5)),
                    "Lê Văn An",
                    "Máy chiếu Epson",
                    "Tốt",
                    fmt_date(t + timedelta(days=1)),
                    "",
                    "",
                    "",
                    "TB001",
                    1,
                    "P-20261001-01",
                    "",
                ],
                # Bình mượn 3 Arduino, quá hạn 4 ngày
                [
                    fmt_date(t - timedelta(days=10)),
                    "Phạm Thị Bình",
                    "Arduino Uno",
                    "Tốt",
                    fmt_date(t - timedelta(days=4)),
                    "",
                    "",
                    "",
                    "TB002",
                    3,
                    "P-20261002-01",
                    "",
                ],
                # dòng cũ của lab: không có mã thiết bị, đã trả, trả kèm hư hỏng trong tuần này
                [
                    fmt_date(t - timedelta(days=30)),
                    "Lê Văn An",
                    "Máy chiếu Epson",
                    "Tốt",
                    fmt_date(t - timedelta(days=20)),
                    fmt_date(week_of(t)[0]),
                    "Hỏng nhẹ",
                    "",
                    "",
                    "",
                    "",
                    "",
                ],
                # tên không khớp Members, đang mượn
                [
                    fmt_date(t - timedelta(days=2)),
                    "Trung Đào",
                    "Arduino Uno",
                    "Tốt",
                    fmt_date(t + timedelta(days=10)),
                    "",
                    "",
                    "",
                    "TB002",
                    1,
                    "P-20261003-01",
                    "",
                ],
            ],
        )
        start, end = week_of(t)
        cur = f"{start:%d/%m}-{end:%d/%m}"
        s.add_tab(
            self.duty_id,
            cur,
            duty_grid(
                f"LỊCH TRỰC LAB TUẦN TỪ {start:%d/%m} - {end:%d/%m}",
                [
                    ("Nguyễn Văn Leader", marks(t2s="T", t4c="T", t6s="v")),
                    ("Trần Thị Thư", marks(t2s="v", t3s="v", t5c="V")),
                    ("Lê Văn An", marks(t3c="v")),
                    ("Phạm Thị Bình", []),
                ],
                numbered=6,
            ),
        )
        s.add_tab(self.tpl_id, "Template", duty_grid("LỊCH TRỰC LAB TUẦN TỪ dd/mm - dd/mm", [], numbered=3))
        s.add_tab(
            self.forms_id,
            "don_xin_gia_nhap",
            [
                [
                    "submission_id",
                    "thoi_gian_nop",
                    "nguoi_nop",
                    "ho_ten",
                    "sdt",
                    "email",
                    "link_file",
                    "trang_thai",
                    "ghi_chu",
                ],
                [
                    "S1",
                    f"{fmt_date(t)} 10:00",
                    "guest",
                    "Hoàng Văn E",
                    "0912345678",
                    "e@gmail.com",
                    "",
                    "CHO_DUYET",
                    "",
                ],
            ],
        )
        s.add_tab(
            self.audit_id,
            "AuditLog",
            [["thoi_gian", "actor", "role", "action", "target", "ket_qua", "chi_tiet"]],
        )

    def next_week_tab(self) -> str:
        a, b = next_week(self.today)
        return f"{a:%d/%m}-{b:%d/%m}"

    def add_kb_doc(self, path: str, data: bytes) -> DriveFile:
        return self.drive.add(path, DOCX, data)

    def audit_rows(self) -> list[list[Any]]:
        return self.sheets.values(self.audit_id, "AuditLog")[1:]
