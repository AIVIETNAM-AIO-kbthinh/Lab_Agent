"""data/Equipment: tab Thietbi + LichSuMuon (TK §3.2).

Số đang mượn / còn lại KHÔNG lưu trên sheet; code tính khi đọc:
    dang_muon(ma_tb) = Σ "Số lượng" các dòng LichSuMuon có Mã thiết bị = ma_tb và "ngày trả" trống
    con_lai(ma_tb)   = tong_so_luong − dang_muon
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import EquipmentSheetCfg
from app.domain.dates import fmt_date, parse_sheet_date
from app.domain.names import fuzzy_match, norm_name
from app.google.paths import PathResolver
from app.google.sheets import SheetsAPI
from app.sheets.table import Row, SheetTable, cell_str


def _int(v: Any, default: int) -> int:
    s = cell_str(v)
    if not s:
        return default
    try:
        return int(float(s.replace(",", ".")))
    except ValueError:
        return default


@dataclass(frozen=True)
class Device:
    row_number: int
    ma_tb: str
    ten_tb: str
    tinh_trang: str
    tong_so_luong: int
    ghi_chu: str = ""
    nguoi_muon: str = ""


@dataclass(frozen=True)
class Borrow:
    row_number: int
    ngay_muon: date | None
    ho_ten: str
    ten_tb: str
    tinh_trang_khi_muon: str
    han_tra: date | None
    ngay_tra: date | None
    tinh_trang_khi_tra: str
    ghi_chu: str
    ma_tb: str
    so_luong: int
    ma_phieu: str
    link_phieu: str

    @property
    def active(self) -> bool:
        return self.ngay_tra is None

    def overdue(self, today: date) -> bool:
        return self.active and self.han_tra is not None and self.han_tra < today

    def days_late(self, today: date) -> int:
        return (today - self.han_tra).days if self.han_tra and self.active else 0


@dataclass
class Availability:
    device: Device
    dang_muon: int
    con_lai: int
    borrowers: list[Borrow] = field(default_factory=list)

    @property
    def borrowable(self) -> bool:
        return self.con_lai > 0


@dataclass
class EquipmentSnapshot:
    devices: list[Device]
    borrows: list[Borrow]
    not_borrowable: list[str] = field(default_factory=list)

    def device(self, ma_tb: str) -> Device | None:
        k = ma_tb.strip().upper()
        return next((d for d in self.devices if d.ma_tb.upper() == k), None)

    def _device_of(self, b: Borrow) -> Device | None:
        if b.ma_tb:
            return self.device(b.ma_tb)
        # dòng cũ của lab chưa có Mã thiết bị: khớp theo tên thiết bị
        k = norm_name(b.ten_tb)
        return next((d for d in self.devices if norm_name(d.ten_tb) == k), None)

    def resolved_ma_tb(self, b: Borrow) -> str:
        d = self._device_of(b)
        return d.ma_tb if d else b.ma_tb

    def search(self, query: str, limit: int = 8) -> list[Device]:
        q = query.strip()
        if not q:
            return list(self.devices)
        d = self.device(q)
        if d:
            return [d]
        hits = fuzzy_match(q, [x.ten_tb for x in self.devices], limit=limit, cutoff=60)
        names = {h for h, _ in hits}
        return [x for x in self.devices if x.ten_tb in names]

    def active_borrows(self, ho_ten: str | None = None) -> list[Borrow]:
        k = norm_name(ho_ten) if ho_ten else None
        return [b for b in self.borrows if b.active and (k is None or norm_name(b.ho_ten) == k)]

    def availability(self, ma_tb: str) -> Availability | None:
        d = self.device(ma_tb)
        if d is None:
            return None
        active = [b for b in self.borrows if b.active and self.resolved_ma_tb(b).upper() == d.ma_tb.upper()]
        dang = sum(b.so_luong for b in active)
        con_lai = max(0, d.tong_so_luong - dang)
        if d.tinh_trang in self.not_borrowable:
            con_lai = 0
        return Availability(d, dang, con_lai, active)

    def overdue(self, today: date) -> list[Borrow]:
        return [b for b in self.borrows if b.overdue(today)]

    def due_on(self, day: date) -> list[Borrow]:
        return [b for b in self.borrows if b.active and b.han_tra == day]

    def render_nguoi_muon_cell(self, ma_tb: str, date_format: str = "%d/%m/%Y") -> str:
        av = self.availability(ma_tb)
        if av is None:
            return ""
        return "\n".join(
            f"{b.ho_ten} - {b.so_luong} - {fmt_date(b.ngay_muon, date_format)} - {fmt_date(b.han_tra, date_format)}"
            for b in sorted(av.borrowers, key=lambda x: (x.ngay_muon or date.min, x.ho_ten))
        )


class EquipmentRepo:
    def __init__(self, sheets: SheetsAPI, paths: PathResolver, cfg: EquipmentSheetCfg) -> None:
        self.sheets = sheets
        self.paths = paths
        self.cfg = cfg

    async def _sid(self) -> str:
        return await self.paths.file_id(self.cfg.path)

    async def devices_table(self) -> SheetTable:
        c = self.cfg.devices
        return await SheetTable(
            self.sheets,
            await self._sid(),
            c.tab,
            c.columns,
            c.header_row,
            optional=("ghi_chu", "nguoi_muon", "con_lai"),
        ).load()

    async def borrows_table(self) -> SheetTable:
        c = self.cfg.borrows
        return await SheetTable(
            self.sheets,
            await self._sid(),
            c.tab,
            c.columns,
            c.header_row,
            optional=(
                "tinh_trang_khi_tra",
                "ghi_chu",
                "ma_tb",
                "so_luong",
                "ma_phieu",
                "link_phieu",
                "tinh_trang_khi_muon",
            ),
        ).load()

    async def snapshot(self) -> EquipmentSnapshot:
        dt, bt = await self.devices_table(), await self.borrows_table()
        return EquipmentSnapshot(
            devices=[d for d in (self._device(r) for r in dt.rows()) if d],
            borrows=[b for b in (self._borrow(r) for r in bt.rows()) if b],
            not_borrowable=list(self.cfg.devices.not_borrowable),
        )

    @staticmethod
    def _device(r: Row) -> Device | None:
        ma = r.text("ma_tb")
        if not ma:
            return None
        return Device(
            r.row_number,
            ma,
            r.text("ten_tb"),
            r.text("tinh_trang"),
            _int(r.get("tong_so_luong"), 0),
            r.text("ghi_chu"),
            r.text("nguoi_muon"),
        )

    def _borrow(self, r: Row) -> Borrow | None:
        ho_ten = r.text("ho_ten")
        if not ho_ten and not r.text("ten_tb"):
            return None
        return Borrow(
            row_number=r.row_number,
            ngay_muon=parse_sheet_date(r.get("ngay_muon")),
            ho_ten=ho_ten,
            ten_tb=r.text("ten_tb"),
            tinh_trang_khi_muon=r.text("tinh_trang_khi_muon"),
            han_tra=parse_sheet_date(r.get("han_tra")),
            ngay_tra=parse_sheet_date(r.get("ngay_tra")),
            tinh_trang_khi_tra=r.text("tinh_trang_khi_tra"),
            ghi_chu=r.text("ghi_chu"),
            ma_tb=r.text("ma_tb"),
            so_luong=_int(r.get("so_luong"), self.cfg.borrows.default_so_luong),
            ma_phieu=r.text("ma_phieu"),
            link_phieu=r.text("link_phieu"),
        )

    # ------------------------------------------------------------ ghi (luồng P3 gọi qua outbox)

    def _fmt(self, d: date) -> str:
        return fmt_date(d, self.cfg.borrows.date_format)

    async def append_borrow_rows(self, rows: list[dict[str, Any]]) -> list[int]:
        """Mỗi thiết bị một dòng. Bỏ qua dòng đã tồn tại (ma_phieu, ma_tb) để idempotent."""
        t = await self.borrows_table()
        existing = {(r.text("ma_phieu"), r.text("ma_tb").upper()) for r in t.rows() if r.text("ma_phieu")}
        todo = []
        for v in rows:
            if (str(v.get("ma_phieu", "")), str(v.get("ma_tb", "")).upper()) in existing:
                continue
            todo.append({k: (self._fmt(x) if isinstance(x, date) else x) for k, x in v.items() if t.has(k)})
        return await t.append_many(todo)

    async def mark_returned(self, row_number: int, ngay_tra: date, tinh_trang: str) -> None:
        t = await self.borrows_table()
        vals: dict[str, Any] = {"ngay_tra": self._fmt(ngay_tra)}
        if t.has("tinh_trang_khi_tra"):
            vals["tinh_trang_khi_tra"] = tinh_trang
        await t.update(row_number, vals)

    async def split_partial_return(
        self, b: Borrow, so_luong_tra: int, ngay_tra: date, tinh_trang: str
    ) -> int:
        """Trả một phần: thêm dòng mới cùng phiếu với số còn lại (ngày trả trống), rồi dòng cũ ghi số đã trả +
        ngày trả. Dòng mới mang dấu "[tách từ dòng N]" trong Ghi chú để chạy lại không tách hai lần."""
        if not 0 < so_luong_tra < b.so_luong:
            raise ValueError("so_luong_tra phải nhỏ hơn số đang mượn")
        t = await self.borrows_table()
        marker = f"[tách từ dòng {b.row_number}]"
        rest_row = next((r for r in t.rows() if marker in r.text("ghi_chu")), None)
        if rest_row is None:
            rest = {
                "ngay_muon": self._fmt(b.ngay_muon) if b.ngay_muon else "",
                "ho_ten": b.ho_ten,
                "ten_tb": b.ten_tb,
                "tinh_trang_khi_muon": b.tinh_trang_khi_muon,
                "han_tra": self._fmt(b.han_tra) if b.han_tra else "",
                "ghi_chu": f"{b.ghi_chu}; {marker}" if b.ghi_chu else marker,
                "ma_tb": b.ma_tb,
                "so_luong": b.so_luong - so_luong_tra,
                "ma_phieu": b.ma_phieu,
                "link_phieu": b.link_phieu,
            }
            rn = await t.append({k: v for k, v in rest.items() if t.has(k)})
        else:
            rn = rest_row.row_number
        await t.update(
            b.row_number,
            {
                "so_luong": so_luong_tra,
                "ngay_tra": self._fmt(ngay_tra),
                **({"tinh_trang_khi_tra": tinh_trang} if t.has("tinh_trang_khi_tra") else {}),
            },
        )
        return rn

    async def return_row(self, row_number: int, so_luong_tra: int, ngay_tra: date, tinh_trang: str) -> str:
        """Trả (toàn bộ hoặc một phần) MỘT dòng LichSuMuon. Idempotent: dòng đã có ngày trả thì bỏ qua."""
        snap = await self.snapshot()
        b = next((x for x in snap.borrows if x.row_number == row_number), None)
        if b is None:
            raise ValueError(f"Không thấy dòng {row_number} trong LichSuMuon")
        if not b.active:
            return "skipped"
        if so_luong_tra >= b.so_luong:
            await self.mark_returned(row_number, ngay_tra, tinh_trang)
            return "returned"
        await self.split_partial_return(b, so_luong_tra, ngay_tra, tinh_trang)
        return "partial"

    async def note_device(self, ma_tb: str, note: str, new_status: str | None = None) -> None:
        """Ghi chú hư hỏng/mất vào Thietbi (thêm vào Ghi chú, idempotent) và đổi Trạng thái hoạt động nếu cần."""
        dt = await self.devices_table()
        row = next((r for r in dt.rows() if r.text("ma_tb").upper() == ma_tb.upper()), None)
        if row is None:
            return
        vals: dict[str, Any] = {}
        if new_status and row.text("tinh_trang") != new_status:
            vals["tinh_trang"] = new_status
        if dt.has("ghi_chu") and note not in row.text("ghi_chu"):
            vals["ghi_chu"] = f"{row.text('ghi_chu')}; {note}" if row.text("ghi_chu") else note
        if vals:
            await dt.update(row.row_number, vals)

    async def extend(self, b: Borrow, new_han_tra: date, today: date) -> None:
        t = await self.borrows_table()
        note = f"gia hạn {self._fmt(b.han_tra) if b.han_tra else '?'} → {self._fmt(new_han_tra)} (ngày {self._fmt(today)})"
        ghi_chu = f"{b.ghi_chu}; {note}" if b.ghi_chu else note
        await t.update(
            b.row_number,
            {"han_tra": self._fmt(new_han_tra), **({"ghi_chu": ghi_chu} if t.has("ghi_chu") else {})},
        )

    async def refresh_nguoi_muon(self, ma_tbs: list[str] | None = None) -> None:
        """Ghi lại cột "Người mượn" và "Còn lại" (nếu sheet có) của Thietbi từ LichSuMuon.

        Còn lại = Số lượng − tổng số đang mượn (dòng LichSuMuon có ngày trả trống), không âm. Chỉ ghi ô thay đổi.
        """
        dt = await self.devices_table()
        if not (dt.has("nguoi_muon") or dt.has("con_lai")):
            return
        snap = await self.snapshot()
        current = {r.row_number: r for r in dt.rows()}
        wanted = {m.upper() for m in ma_tbs} if ma_tbs else None
        for d in snap.devices:
            ma = d.ma_tb.upper()
            if wanted is not None and ma not in wanted:
                continue
            av = snap.availability(ma)
            if av is None:
                continue
            vals: dict[str, Any] = {}
            if dt.has("nguoi_muon"):
                cell = snap.render_nguoi_muon_cell(ma, self.cfg.borrows.date_format)
                if cell != d.nguoi_muon:
                    vals["nguoi_muon"] = cell
            if dt.has("con_lai"):
                remaining = max(0, d.tong_so_luong - av.dang_muon)
                row = current.get(d.row_number)
                if row is None or _int(row.get("con_lai"), -1) != remaining:
                    vals["con_lai"] = remaining
            if vals:
                await dt.update(d.row_number, vals)


def group_by_borrower(borrows: list[Borrow]) -> dict[str, list[Borrow]]:
    out: dict[str, list[Borrow]] = defaultdict(list)
    for b in borrows:
        out[b.ho_ten].append(b)
    return dict(out)
