"""Lọc dữ liệu từ các sheet lớn (Members, Thietbi, LichSuMuon, DutySchedule) thành bảng nhỏ để xuất file.

Quyền xem giữ đúng như các tool tra cứu: SĐT, MSSV và cựu thành viên chỉ thư ký/leader/admin thấy;
Address (và cột trong `never_expose`) không bao giờ xuất. Thành viên thường chỉ xuất được lịch sử mượn
(đã trả) của chính mình; danh sách đang mượn thì ai trong lab cũng xem được (như `query_equipment`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app import clock
from app.auth.roles import STAFF, UserContext
from app.domain.dates import fmt_date, resolve_time_expr, week_of
from app.domain.equipment import Borrow, EquipmentSnapshot
from app.domain.members import Member
from app.domain.names import ascii_key, fuzzy_match, norm_name

if TYPE_CHECKING:
    from app.services import Services

Dataset = Literal["members", "devices", "borrows", "duty"]
RoleKey = Literal["leader", "thu_ki", "member"]


class ExportError(ValueError):
    """Yêu cầu xuất không hợp lệ; `details` trả lại cho LLM để hỏi lại người dùng."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: Literal["text", "int", "date"] = "text"
    width: int = 18
    staff_only: bool = False


MEMBER_COLUMNS = [
    Column("stt", "STT", "int", 6),
    Column("ho_ten", "Họ tên", width=26),
    Column("mssv", "MSSV", width=12, staff_only=True),
    Column("vai_tro", "Vai trò", width=12),
    Column("email", "Email", width=28),
    Column("sdt", "Số điện thoại", width=14, staff_only=True),
    Column("lop", "Lớp", width=10),
    Column("truong", "Trường", width=14),
    Column("trang_thai", "Trạng thái", width=16),
]
DEVICE_COLUMNS = [
    Column("ma_tb", "Mã thiết bị", width=12),
    Column("ten_tb", "Tên thiết bị", width=26),
    Column("tinh_trang", "Tình trạng", width=14),
    Column("tong_so_luong", "Tổng số lượng", "int", 10),
    Column("dang_muon", "Đang mượn", "int", 10),
    Column("con_lai", "Còn lại", "int", 10),
    Column("nguoi_dang_muon", "Người đang mượn", width=40),
    Column("ghi_chu", "Ghi chú", width=24),
]
BORROW_COLUMNS = [
    Column("ma_phieu", "Mã phiếu", width=16),
    Column("ho_ten", "Người mượn", width=24),
    Column("ma_tb", "Mã thiết bị", width=12),
    Column("ten_tb", "Tên thiết bị", width=24),
    Column("so_luong", "Số lượng", "int", 9),
    Column("ngay_muon", "Ngày mượn", "date", 12),
    Column("han_tra", "Hạn trả", "date", 12),
    Column("ngay_tra", "Ngày trả", "date", 12),
    Column("trang_thai", "Trạng thái", width=12),
    Column("tre_ngay", "Số ngày trễ", "int", 10),
    Column("tinh_trang_khi_muon", "Tình trạng khi mượn", width=16),
    Column("tinh_trang_khi_tra", "Tình trạng khi trả", width=16),
    Column("ghi_chu", "Ghi chú", width=28),
]
DUTY_COLUMNS = [
    Column("tuan", "Tuần", width=14),
    Column("ngay", "Ngày", "date", 12),
    Column("thu", "Thứ", width=10),
    Column("buoi", "Buổi", width=8),
    Column("ho_ten", "Họ tên", width=26),
    Column("loai", "Loại", width=14),
]
CATALOG: dict[str, list[Column]] = {
    "members": MEMBER_COLUMNS,
    "devices": DEVICE_COLUMNS,
    "borrows": BORROW_COLUMNS,
    "duty": DUTY_COLUMNS,
}
MAX_DUTY_WEEKS = 12


def column_help() -> str:
    return "; ".join(f"{ds}: {', '.join(c.key for c in cols)}" for ds, cols in CATALOG.items())


class ExportRequest(BaseModel):
    dataset: Dataset = Field(
        description="members = danh sách thành viên; devices = danh sách thiết bị (kèm số đang mượn/còn lại); "
        "borrows = các lượt mượn thiết bị (đang mượn, quá hạn, đã trả); duty = lịch trực lab (ai trực buổi nào)."
    )
    format: Literal["xlsx", "csv"] = Field("xlsx", description="Định dạng file. Mặc định Excel (xlsx).")
    columns: list[str] | None = Field(
        None,
        description="CHỈ khi người dùng yêu cầu chọn cột: mã cột theo thứ tự muốn xuất. Mã hợp lệ — "
        + column_help()
        + ". Bỏ trống = các cột mặc định.",
        max_length=20,
    )
    # members
    member_status: Literal["active", "former", "all"] | None = Field(
        None,
        description="[members] active = đang hoạt động (mặc định), former = cựu thành viên, all = cả hai.",
    )
    roles: list[RoleKey] | None = Field(
        None, description="[members] Chỉ lấy các vai trò này (leader, thu_ki = thư ký, member)."
    )
    truong: str | None = Field(None, description="[members] Lọc theo trường, vd 'UET'.", max_length=100)
    lop: str | None = Field(None, description="[members] Lọc theo lớp/khoá, vd 'K67'.", max_length=50)
    # devices + borrows
    device: str | None = Field(
        None, description="[devices, borrows] Tên hoặc mã thiết bị cần lọc (khớp gần đúng).", max_length=200
    )
    # devices
    tinh_trang: str | None = Field(
        None, description="[devices] Lọc theo tình trạng thiết bị, vd 'Tốt', 'Hỏng'.", max_length=50
    )
    availability: Literal["available", "borrowed", "not_borrowable"] | None = Field(
        None,
        description="[devices] available = còn cho mượn được; borrowed = đang có người mượn; "
        "not_borrowable = không cho mượn (hỏng/thanh lý...).",
    )
    # borrows
    borrow_status: Literal["active", "overdue", "returned", "all"] | None = Field(
        None,
        description="[borrows] active = đang mượn chưa trả (mặc định), overdue = quá hạn chưa trả, "
        "returned = đã trả, all = toàn bộ lịch sử.",
    )
    borrower: str | None = Field(
        None, description="[borrows] Họ tên người mượn (khớp gần đúng).", max_length=200
    )
    time_expr: str | None = Field(
        None,
        description="[borrows] Lọc theo NGÀY MƯỢN; [duty] các tuần cần xuất (mặc định tuần này). Truyền NGUYÊN "
        "VĂN biểu thức thời gian của người dùng, vd 'tháng 9', 'tuần trước', 'tuần sau', '01/09 - 15/10'.",
        max_length=100,
    )
    duty_member: str | None = Field(
        None,
        description="[duty] Chỉ lấy lịch của người này (họ tên, khớp gần đúng). 'Của tôi' = họ tên người hỏi.",
        max_length=200,
    )
    filename: str | None = Field(
        None, description="Tên file CHỈ khi người dùng tự đặt (không cần đuôi).", max_length=80
    )


@dataclass
class Table:
    dataset: str
    title: str  # tiêu đề sheet / mô tả ngắn
    file_stem: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    filters: dict[str, str] = field(default_factory=dict)  # bộ lọc đã áp dụng (nhãn → giá trị), ghi vào file
    notes: list[str] = field(default_factory=list)  # lưu ý cho LLM báo lại người dùng
    created_at: datetime = field(default_factory=clock.now)


# ------------------------------------------------------------------ helpers


def word_in(query: str, value: str) -> bool:
    """So khớp không dấu, theo nguyên từ: 'K67' khớp 'K67', 'uet' khớp 'ĐH Công nghệ - UET'."""
    q, v = ascii_key(query), ascii_key(value)
    return bool(q) and re.search(rf"(?<!\w){re.escape(q)}(?!\w)", v) is not None


async def known_people(svc: Services) -> list[str]:
    """Họ tên mọi người hệ thống biết (thành viên, cựu thành viên, admin) để so tên khi lọc theo người.
    Không đọc được Members thì trả danh sách admin (pick_name vẫn chạy trên tên có trong dữ liệu)."""
    names = [a.ho_ten for a in svc.config.auth.admins if a.ho_ten]
    try:
        snap = await svc.members.snapshot()
    except Exception:
        return names
    return names + [m.ho_ten for m in [*snap.active, *snap.former] if m.ho_ten]


def pick_name(
    query: str,
    names: list[str],
    what: str,
    *,
    known: list[str] | None = None,
    notes: list[str] | None = None,
) -> str:
    """Chọn người theo tên người dùng nói.

    So với `names` (người có dữ liệu) CỘNG `known` (mọi người hệ thống biết). Nhờ vậy tên của một người có thật
    nhưng chưa có dữ liệu sẽ khớp đúng người đó (lọc ra 0 dòng), không bị đoán sang người khác có tên gần giống.
    """
    pool = list(dict.fromkeys([*names, *(known or [])]))
    hits = fuzzy_match(query, pool, limit=3)
    if not hits:
        raise ExportError(f"Không tìm thấy {what} khớp '{query}'.")
    if len(hits) > 1 and hits[0][1] < 100 and hits[1][1] >= hits[0][1] - 5:
        raise ExportError(
            f"Có nhiều {what} tên gần giống '{query}', hãy hỏi lại người dùng muốn chọn ai.",
            ambiguous=[h for h, _ in hits],
        )
    who, score = hits[0]
    if score < 100 and notes is not None:
        notes.append(f"Không có tên trùng khớp '{query}', đã hiểu là '{who}'. Báo rõ cho người dùng.")
    return who


def _resolve_columns(
    req: ExportRequest, ctx: UserContext, defaults: list[str], notes: list[str]
) -> list[Column]:
    catalog = {c.key: c for c in CATALOG[req.dataset]}
    keys = list(dict.fromkeys(req.columns)) if req.columns else defaults
    unknown = [k for k in keys if k not in catalog]
    if unknown:
        raise ExportError(
            f"Không có cột {', '.join(unknown)} cho dữ liệu này.",
            valid_columns={c.key: c.label for c in CATALOG[req.dataset]},
        )
    cols = [catalog[k] for k in keys]
    if ctx.role not in STAFF:
        hidden = [c.label for c in cols if c.staff_only]
        cols = [c for c in cols if not c.staff_only]
        if hidden and req.columns:
            notes.append(f"Cột {', '.join(hidden)} chỉ thư ký/leader/admin được xuất nên đã bỏ.")
    if not cols:
        raise ExportError(
            "Không còn cột nào để xuất.", valid_columns={k: c.label for k, c in catalog.items()}
        )
    return cols


def _today_str() -> str:
    return fmt_date(clock.today(), "%d-%m-%Y")


# ------------------------------------------------------------------ members


async def _members(svc: Services, ctx: UserContext, req: ExportRequest) -> Table:
    notes: list[str] = []
    status = req.member_status or "active"
    if status != "active" and ctx.role not in STAFF:
        raise ExportError("Chỉ thư ký, leader hoặc admin được xuất danh sách cựu thành viên.")
    snap = await svc.members.snapshot()
    labels = svc.config.sheets.members.role_labels
    pool: list[tuple[Member, str]] = []
    if status in ("active", "all"):
        pool += [(m, "Đang hoạt động") for m in snap.active]
    if status in ("former", "all"):
        pool += [(m, "Cựu thành viên") for m in snap.former]

    filters: dict[str, str] = {
        "Trạng thái": {"active": "Đang hoạt động", "former": "Cựu thành viên", "all": "Tất cả"}[status]
    }
    if req.roles:
        wanted = set(req.roles)
        pool = [(m, s) for m, s in pool if m.role in wanted]
        filters["Vai trò"] = ", ".join(labels.get(r, r) for r in req.roles)
    if req.truong:
        pool = [(m, s) for m, s in pool if word_in(req.truong, m.truong)]
        filters["Trường"] = req.truong
    if req.lop:
        pool = [(m, s) for m, s in pool if word_in(req.lop, m.lop)]
        filters["Lớp"] = req.lop

    defaults = ["stt", "ho_ten", "mssv", "vai_tro", "email", "sdt", "lop", "truong"]
    if status == "all":
        defaults.append("trang_thai")
    rows = [
        {
            "stt": i,
            "ho_ten": m.ho_ten,
            "mssv": m.mssv,
            "vai_tro": labels.get(m.role, m.role),
            "email": m.email,
            "sdt": m.sdt,
            "lop": m.lop,
            "truong": m.truong,
            "trang_thai": s,
        }
        for i, (m, s) in enumerate(pool, start=1)
    ]
    stem = {"active": "DanhSachThanhVien", "former": "CuuThanhVien", "all": "ThanhVienVaCuuThanhVien"}[status]
    return Table(
        "members",
        {"active": "Thành viên", "former": "Cựu thành viên", "all": "Thành viên"}[status],
        f"{stem}_{_today_str()}",
        _resolve_columns(req, ctx, defaults, notes),
        rows,
        filters,
        notes,
    )


# ------------------------------------------------------------------ devices


def _device_codes(snap: EquipmentSnapshot, query: str) -> set[str]:
    found = snap.search(query, limit=max(len(snap.devices), 1))
    if not found:
        raise ExportError(f"Không tìm thấy thiết bị khớp '{query}'.")
    return {d.ma_tb.upper() for d in found}


async def _devices(svc: Services, ctx: UserContext, req: ExportRequest) -> Table:
    notes: list[str] = []
    snap = await svc.equipment.snapshot()
    today = clock.today()
    devices = list(snap.devices)
    filters: dict[str, str] = {}
    if req.device:
        codes = _device_codes(snap, req.device)
        devices = [d for d in devices if d.ma_tb.upper() in codes]
        filters["Thiết bị"] = req.device
    if req.tinh_trang:
        all_status = sorted({d.tinh_trang for d in snap.devices if d.tinh_trang})
        devices = [d for d in devices if word_in(req.tinh_trang, d.tinh_trang)]
        filters["Tình trạng"] = req.tinh_trang
        if not devices:
            notes.append(f"Các tình trạng đang có: {', '.join(all_status)}.")

    rows = []
    for d in devices:
        av = snap.availability(d.ma_tb)
        if av is None:
            continue
        not_borrowable = d.tinh_trang in snap.not_borrowable
        if req.availability == "available" and (not_borrowable or av.con_lai <= 0):
            continue
        if req.availability == "borrowed" and av.dang_muon <= 0:
            continue
        if req.availability == "not_borrowable" and not not_borrowable:
            continue
        rows.append(
            {
                "ma_tb": d.ma_tb,
                "ten_tb": d.ten_tb,
                "tinh_trang": d.tinh_trang,
                "tong_so_luong": d.tong_so_luong,
                "dang_muon": av.dang_muon,
                "con_lai": av.con_lai,
                "nguoi_dang_muon": "\n".join(
                    f"{b.ho_ten} ({b.so_luong}, hạn {fmt_date(b.han_tra)}{', QUÁ HẠN' if b.overdue(today) else ''})"
                    for b in av.borrowers
                ),
                "ghi_chu": d.ghi_chu,
            }
        )
    if req.availability:
        filters["Khả năng mượn"] = {
            "available": "Còn cho mượn được",
            "borrowed": "Đang có người mượn",
            "not_borrowable": "Không cho mượn",
        }[req.availability]
    defaults = [c.key for c in DEVICE_COLUMNS]
    return Table(
        "devices",
        "Thiết bị",
        f"DanhSachThietBi_{_today_str()}",
        _resolve_columns(req, ctx, defaults, notes),
        rows,
        filters,
        notes,
    )


# ------------------------------------------------------------------ borrows


def _borrow_state(b: Borrow, today: date) -> str:
    if not b.active:
        return "Đã trả"
    return "Quá hạn" if b.overdue(today) else "Đang mượn"


async def _borrows(svc: Services, ctx: UserContext, req: ExportRequest) -> Table:
    notes: list[str] = []
    snap = await svc.equipment.snapshot()
    today = clock.today()
    status = req.borrow_status or "active"
    borrows = list(snap.borrows)
    filters: dict[str, str] = {
        "Trạng thái": {
            "active": "Đang mượn (chưa trả)",
            "overdue": "Quá hạn chưa trả",
            "returned": "Đã trả",
            "all": "Toàn bộ lịch sử",
        }[status]
    }
    if status == "active":
        borrows = [b for b in borrows if b.active]
    elif status == "overdue":
        borrows = [b for b in borrows if b.overdue(today)]
    elif status == "returned":
        borrows = [b for b in borrows if not b.active]

    borrower = None
    if req.borrower:
        borrower = pick_name(
            req.borrower,
            sorted({b.ho_ten for b in snap.borrows if b.ho_ten}),
            "người mượn",
            known=await known_people(svc),
            notes=notes,
        )
    if status in ("returned", "all") and ctx.role not in STAFF:
        # lịch sử đã trả của người khác: chỉ thư ký/leader/admin
        if not ctx.ho_ten:
            raise ExportError(
                "Tài khoản của bạn không gắn với thành viên nào nên không xuất được lịch sử mượn."
            )
        if borrower and norm_name(borrower) != norm_name(ctx.ho_ten):
            raise ExportError("Thành viên chỉ xuất được lịch sử mượn của chính mình.")
        if borrower is None:
            notes.append(
                "Thành viên chỉ xuất được lịch sử mượn của chính mình nên file chỉ gồm các lượt của bạn."
            )
        borrower = ctx.ho_ten
    if borrower:
        borrows = [b for b in borrows if norm_name(b.ho_ten) == norm_name(borrower)]
        filters["Người mượn"] = borrower
    if req.device:
        codes = _device_codes(snap, req.device)
        borrows = [b for b in borrows if snap.resolved_ma_tb(b).upper() in codes]
        filters["Thiết bị"] = req.device
    if req.time_expr:
        rng = resolve_time_expr(req.time_expr, today)
        if rng is None:
            raise ExportError(f"Không hiểu biểu thức thời gian '{req.time_expr}'.")
        start, end = rng
        borrows = [b for b in borrows if b.ngay_muon is not None and start <= b.ngay_muon <= end]
        filters["Ngày mượn"] = f"{fmt_date(start)} – {fmt_date(end)}"

    if status == "overdue":
        borrows.sort(key=lambda b: (b.han_tra or date.min, norm_name(b.ho_ten)))
    else:
        borrows.sort(key=lambda b: (b.ngay_muon or date.min, norm_name(b.ho_ten)))
    rows = [
        {
            "ma_phieu": b.ma_phieu,
            "ho_ten": b.ho_ten,
            "ma_tb": snap.resolved_ma_tb(b),
            "ten_tb": b.ten_tb,
            "so_luong": b.so_luong,
            "ngay_muon": b.ngay_muon,
            "han_tra": b.han_tra,
            "ngay_tra": b.ngay_tra,
            "trang_thai": _borrow_state(b, today),
            "tre_ngay": b.days_late(today) if b.overdue(today) else None,
            "tinh_trang_khi_muon": b.tinh_trang_khi_muon,
            "tinh_trang_khi_tra": b.tinh_trang_khi_tra,
            "ghi_chu": b.ghi_chu,
        }
        for b in borrows
    ]
    if status in ("active", "overdue"):
        defaults = [
            "ma_phieu",
            "ho_ten",
            "ma_tb",
            "ten_tb",
            "so_luong",
            "ngay_muon",
            "han_tra",
            "trang_thai",
            "tre_ngay",
            "ghi_chu",
        ]
    else:
        defaults = [c.key for c in BORROW_COLUMNS if c.key != "tre_ngay"]
    stem = {
        "active": "ThietBiDangMuon",
        "overdue": "ThietBiQuaHan",
        "returned": "LichSuTraThietBi",
        "all": "LichSuMuonThietBi",
    }[status]
    title = {"active": "Đang mượn", "overdue": "Quá hạn", "returned": "Đã trả", "all": "Lịch sử mượn"}[status]
    return Table(
        "borrows",
        title,
        f"{stem}_{_today_str()}",
        _resolve_columns(req, ctx, defaults, notes),
        rows,
        filters,
        notes,
    )


# ------------------------------------------------------------------ duty (C9)


async def _duty(svc: Services, ctx: UserContext, req: ExportRequest) -> Table:
    today = clock.today()
    filters: dict[str, str] = {}
    notes: list[str] = []
    if req.time_expr:
        rng = resolve_time_expr(req.time_expr, today)
        if rng is None:
            raise ExportError(f"Không hiểu biểu thức thời gian '{req.time_expr}'.")
        first, last = week_of(rng[0])[0], rng[1]
    else:
        first, last = week_of(today)
    weeks: list[tuple[date, date]] = []
    d = first
    while d <= last and len(weeks) < MAX_DUTY_WEEKS:
        weeks.append((d, d + timedelta(days=6)))
        d += timedelta(days=7)
    if d <= last:
        notes.append(f"Chỉ xuất tối đa {MAX_DUTY_WEEKS} tuần; hãy lọc hẹp hơn nếu cần thêm.")
    filters["Tuần"] = f"{fmt_date(weeks[0][0])} – {fmt_date(weeks[-1][1])}"
    cfg = svc.config.sheets.duty
    counted = {m.casefold() for m in cfg.marks_counted}
    fixed = cfg.fixed_mark.casefold()
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for start, end in weeks:
        tab = await svc.duty.load_week(start, end)
        if tab is None:
            missing.append(f"{fmt_date(start, '%d/%m')}–{fmt_date(end, '%d/%m')}")
            continue
        for slot in tab.slots:
            for r in tab.rows:
                v = str(r.marks.get(slot.col) or "").strip().casefold()
                if v not in counted:
                    continue
                rows.append(
                    {
                        "tuan": tab.title,
                        "ngay": start + timedelta(days=slot.day),
                        "thu": ("Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật")[slot.day],
                        "buoi": slot.label,
                        "ho_ten": r.name,
                        "loai": "Lab giao" if v == fixed else "Đăng ký",
                    }
                )
    if missing:
        notes.append(f"Chưa có tab lịch trực cho tuần: {', '.join(missing)}.")
    if req.duty_member:
        names = sorted({r["ho_ten"] for r in rows})
        if not names:
            raise ExportError("Không có ai đăng ký trực trong khoảng thời gian này.")
        who = pick_name(req.duty_member, names, "thành viên", known=await known_people(svc), notes=notes)
        rows = [r for r in rows if norm_name(r["ho_ten"]) == norm_name(who)]
        filters["Thành viên"] = who
    defaults = [c.key for c in DUTY_COLUMNS]
    stem = "LichTruc" + (f"_{fmt_date(weeks[0][0], '%d-%m')}" if len(weeks) == 1 else "") + f"_{_today_str()}"
    return Table("duty", "Lịch trực", stem, _resolve_columns(req, ctx, defaults, notes), rows, filters, notes)


BUILDERS = {"members": _members, "devices": _devices, "borrows": _borrows, "duty": _duty}

# tham số chỉ có nghĩa với một loại dữ liệu: dùng sai thì báo để LLM không tưởng là đã lọc
_SCOPED = {
    "member_status": "members",
    "roles": "members",
    "truong": "members",
    "lop": "members",
    "tinh_trang": "devices",
    "availability": "devices",
    "borrow_status": "borrows",
    "borrower": "borrows",
    "time_expr": "borrows,duty",
    "duty_member": "duty",
}


async def build_table(svc: Services, ctx: UserContext, req: ExportRequest) -> Table:
    misplaced = [k for k, ds in _SCOPED.items() if getattr(req, k) and req.dataset not in ds.split(",")]
    if req.device and req.dataset == "members":
        misplaced.append("device")
    if misplaced:
        raise ExportError(
            f"Tham số {', '.join(misplaced)} không dùng được với dữ liệu '{req.dataset}'.",
            hint="Bỏ các tham số đó hoặc đổi dataset cho đúng.",
        )
    table = await BUILDERS[req.dataset](svc, ctx, req)
    if req.filename:
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", req.filename).strip(" ._")
        if stem:
            table.file_stem = stem[:80]
    return table
