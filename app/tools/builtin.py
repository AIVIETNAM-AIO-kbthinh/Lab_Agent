"""Tool nghiệp vụ đọc dữ liệu (P2). Không tool nào nhận đường dẫn Drive tuỳ ý; không có tool xác nhận/xoá.

Mọi phép đếm, cộng trừ, chuyển biểu thức thời gian đều do code làm (nguyên tắc 1, §6.1).
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app import clock
from app.auth.roles import ALL_ROLES, BORROWERS, LEADER_ADMIN, MEMBER_PLUS, STAFF, Role, UserContext
from app.domain.dates import fmt_date, resolve_time_expr, week_of
from app.domain.duty import tab_link
from app.domain.names import fuzzy_match
from app.tools.registry import REGISTRY, ToolResult, untrusted

if TYPE_CHECKING:
    from app.services import Services

ALL = ALL_ROLES
DAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]


# ------------------------------------------------------------------ search_kb


class SearchKbArgs(BaseModel):
    query: str = Field(
        description="Câu truy vấn tìm kiếm, tiếng Việt hoặc tiếng Anh.", min_length=1, max_length=500
    )
    doc_type: Literal["rule", "minutes", "web", "other"] | None = Field(
        None,
        description="Lọc loại tài liệu: rule=nội quy/quy định, minutes=biên bản họp, web=nội dung website lab.",
    )
    time_expr: str | None = Field(
        None,
        description="Biểu thức thời gian NGUYÊN VĂN của người dùng để lọc theo ngày tài liệu, "
        "vd 'tuần trước', 'tháng 9', '12/10 - 18/10'. Bỏ trống nếu không có.",
    )


@REGISTRY.tool(
    "search_kb",
    "Tìm trong kho tài liệu của lab (nội quy, giới thiệu, website lab; với thành viên còn có tài liệu nội "
    "bộ và biên bản họp). Dùng cho câu hỏi về thông tin phi cấu trúc. KHÔNG dùng cho thiết bị, lịch trực, "
    "danh sách thành viên.",
    ALL,
    SearchKbArgs,
)
async def search_kb(svc: Services, ctx: UserContext, a: SearchKbArgs) -> ToolResult:
    if a.doc_type == "minutes" and ctx.role == Role.GUEST:
        return ToolResult({"results": [], "note": "Không có tài liệu phù hợp."})
    date_from = date_to = None
    note = None
    if a.time_expr:
        rng = resolve_time_expr(a.time_expr, clock.today())
        if rng:
            date_from, date_to = rng
        else:
            note = f"Không hiểu biểu thức thời gian '{a.time_expr}', đã tìm không lọc ngày."
    res = await svc.retriever.search(
        a.query, ctx.role, doc_type=a.doc_type, date_from=date_from, date_to=date_to
    )
    items = [
        {
            "title": h.payload.get("title"),
            "section": h.payload.get("heading") or None,
            "source": h.payload.get("location"),
            "doc_date": h.payload.get("doc_date"),
            "type": h.payload.get("doc_type"),
            "text": h.text,
        }
        for h in res.hits
    ]
    out: dict[str, Any] = {"results": untrusted(items), "count": len(items)}
    if date_from:
        out["date_range"] = f"{fmt_date(date_from)} – {fmt_date(date_to)}"
    if note:
        out["note"] = note
    if not items:
        out["note"] = (note + " " if note else "") + "Không tìm thấy tài liệu phù hợp."
    return ToolResult(out)


# ------------------------------------------------------------------ list_forms


@REGISTRY.tool(
    "list_forms", "Danh sách các loại đơn lab đang dùng mà người hỏi được phép nộp, kèm mô tả.", ALL
)
async def list_forms(svc: Services, ctx: UserContext, _: BaseModel) -> ToolResult:
    defs = await svc.forms.for_role(ctx.role.value)
    forms = [
        {"form_id": d.form_id, "title": d.schema.title, "description": d.schema.description} for d in defs
    ]
    if not forms:
        return ToolResult({"forms": [], "note": "Hiện chưa có loại đơn nào mở cho bạn qua chatbot."})
    return ToolResult(
        {
            "forms": forms,
            "how_to": "Muốn nộp: get_form_template (gửi mẫu + lời mời) rồi start_form để điền hộ, "
            "hoặc submit_filled_form khi người dùng gửi file đã tự điền.",
        }
    )


# ------------------------------------------------------------------ get_application_status


@REGISTRY.tool(
    "get_application_status",
    "Trả lời khi người hỏi muốn biết trạng thái đơn xin gia nhập lab của họ.",
    {Role.GUEST},
)
async def get_application_status(svc: Services, ctx: UserContext, _: BaseModel) -> ToolResult:
    # Câu trả lời CỐ ĐỊNH: không xác nhận hay phủ nhận việc một người cụ thể đã nộp đơn (TK §8.1 bước 6)
    snap = await svc.members.snapshot()
    leader = snap.leader()
    emails = [m.email for m in snap.secretaries() if m.email] or (
        [leader.email] if leader and leader.email else []
    )
    contact = ", ".join(dict.fromkeys(emails)) or "thư ký của lab"
    return ToolResult(
        {
            "answer": f"Đơn của bạn đang trong quá trình xử lý. Nếu muốn biết thêm chi tiết, "
            f"vui lòng liên hệ {contact}."
        }
    )


# ------------------------------------------------------------------ query_equipment


class EquipmentArgs(BaseModel):
    name: str | None = Field(
        None, description="Tên hoặc mã thiết bị (vd 'máy chiếu', 'TB003'). Bỏ trống = tất cả.", max_length=200
    )


@REGISTRY.tool(
    "query_equipment",
    "Tra thiết bị của lab: tổng số, số đang mượn, số còn lại, tình trạng và ai đang mượn.",
    MEMBER_PLUS,
    EquipmentArgs,
)
async def query_equipment(svc: Services, ctx: UserContext, a: EquipmentArgs) -> ToolResult:
    snap = await svc.equipment.snapshot()
    today = clock.today()
    devices = snap.search(a.name or "")
    if not devices:
        return ToolResult({"devices": [], "note": f"Không tìm thấy thiết bị khớp '{a.name}'."})
    out = []
    for d in devices[:15]:
        av = snap.availability(d.ma_tb)
        if av is None:
            continue
        out.append(
            {
                "ma_tb": d.ma_tb,
                "ten_tb": d.ten_tb,
                "tinh_trang": d.tinh_trang,
                "tong_so_luong": d.tong_so_luong,
                "dang_muon": av.dang_muon,
                "con_lai": av.con_lai,
                "cho_muon_duoc": d.tinh_trang not in snap.not_borrowable and av.con_lai > 0,
                "nguoi_dang_muon": [
                    {
                        "ho_ten": b.ho_ten,
                        "so_luong": b.so_luong,
                        "han_tra": fmt_date(b.han_tra),
                        "qua_han": b.overdue(today),
                    }
                    for b in av.borrowers
                ],
                "ghi_chu": d.ghi_chu or None,
            }
        )
    res: dict[str, Any] = {"devices": out}
    if len(devices) > 15:
        res["note"] = f"Có {len(devices)} thiết bị khớp, chỉ hiển thị 15. Hãy hỏi cụ thể hơn."
    return ToolResult(res)


# ------------------------------------------------------------------ my_borrows


@REGISTRY.tool(
    "my_borrows", "Các thiết bị người hỏi đang mượn (chưa trả), nhóm theo mã phiếu, kèm hạn trả.", BORROWERS
)
async def my_borrows(svc: Services, ctx: UserContext, _: BaseModel) -> ToolResult:
    if not ctx.ho_ten:
        return ToolResult(
            {"phieu": [], "note": "Tài khoản của bạn không gắn với thành viên nào trong Members."}
        )
    snap = await svc.equipment.snapshot()
    today = clock.today()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in snap.active_borrows(ctx.ho_ten):
        groups[b.ma_phieu or "(phiếu cũ, không có mã)"].append(
            {
                "ma_tb": snap.resolved_ma_tb(b) or None,
                "ten_tb": b.ten_tb,
                "so_luong": b.so_luong,
                "ngay_muon": fmt_date(b.ngay_muon),
                "han_tra": fmt_date(b.han_tra),
                "qua_han": b.overdue(today),
                "tre_ngay": b.days_late(today) if b.overdue(today) else 0,
            }
        )
    if not groups:
        return ToolResult({"phieu": [], "note": "Bạn không có thiết bị nào đang mượn."})
    return ToolResult({"phieu": [{"ma_phieu": k, "thiet_bi": v} for k, v in groups.items()]})


# ------------------------------------------------------------------ query_duty


class DutyArgs(BaseModel):
    member: str | None = Field(
        None, description="Họ tên thành viên cần tra (khớp gần đúng). Bỏ trống = cả lab.", max_length=200
    )
    week: str | None = Field(
        None,
        description="Tuần cần tra, NGUYÊN VĂN người dùng: 'tuần này', 'tuần sau', "
        "'tuần trước' hoặc một ngày dd/mm. Bỏ trống = tuần này.",
    )


@REGISTRY.tool(
    "query_duty",
    "Tra lịch trực lab theo tuần: ai trực buổi nào, mỗi người đã đăng ký bao nhiêu buổi.",
    MEMBER_PLUS,
    DutyArgs,
)
async def query_duty(svc: Services, ctx: UserContext, a: DutyArgs) -> ToolResult:
    today = clock.today()
    rng = resolve_time_expr(a.week, today) if a.week else None
    start, end = week_of(rng[0] if rng else today)
    tab = await svc.duty.load_week(start, end)
    week_label = f"{fmt_date(start, '%d/%m')} – {fmt_date(end, '%d/%m/%Y')}"
    if tab is None:
        return ToolResult({"week": week_label, "note": "Chưa có tab lịch trực cho tuần này."})
    cfg = svc.config.sheets.duty
    sid = await svc.duty.spreadsheet_id()
    base: dict[str, Any] = {
        "week": week_label,
        "link": tab_link(sid, tab.sheet_id),
        "min_sessions": cfg.min_sessions_per_week,
    }
    if a.member:
        hits = fuzzy_match(a.member, [r.name for r in tab.rows], limit=3)
        if not hits:
            return ToolResult({**base, "note": f"Không thấy '{a.member}' trong lịch trực tuần này."})
        if len(hits) > 1 and hits[0][1] < 100 and hits[1][1] >= hits[0][1] - 5:
            return ToolResult(
                {
                    **base,
                    "ambiguous": [h for h, _ in hits],
                    "note": "Có nhiều người tên gần giống, hãy hỏi lại người dùng muốn tra ai.",
                }
            )
        row = tab.row_by_name(hits[0][0])
        assert row is not None
        slots = [
            {"thu": DAYS[s.day], "buoi": s.label, "danh_dau": row.marks[s.col]}
            for s in tab.slots
            if row.marks.get(s.col, "").casefold() in {m.casefold() for m in cfg.marks_counted}
        ]
        return ToolResult(
            {
                **base,
                "member": row.name,
                "so_buoi": tab.count_sessions(row, cfg.marks_counted),
                "cac_buoi": slots,
            }
        )
    by_slot: dict[str, list[str]] = defaultdict(list)
    counts = []
    marks = {m.casefold() for m in cfg.marks_counted}
    for r in tab.rows:
        counts.append({"ho_ten": r.name, "so_buoi": tab.count_sessions(r, cfg.marks_counted)})
        for s in tab.slots:
            if r.marks.get(s.col, "").casefold() in marks:
                by_slot[f"{DAYS[s.day]} {s.label}"].append(r.name)
    order = [f"{DAYS[s.day]} {s.label}" for s in tab.slots]
    return ToolResult(
        {**base, "theo_buoi": {k: by_slot.get(k, []) for k in order}, "so_buoi_moi_nguoi": counts}
    )


# ------------------------------------------------------------------ query_members


class MembersArgs(BaseModel):
    name: str | None = Field(
        None, description="Họ tên cần tra (khớp gần đúng). Bỏ trống = danh sách thành viên.", max_length=200
    )


@REGISTRY.tool(
    "query_members",
    "Tra thành viên đang hoạt động của lab: họ tên, vai trò, email, lớp, trường. Với thư ký/leader/admin, kết "
    "quả có thêm số điện thoại (sdt) và MSSV (mssv) của mọi người; với thành viên thường chỉ có SĐT, MSSV của "
    "chính họ. Dùng cả khi người hỏi xin SĐT/MSSV: hệ thống tự lọc theo quyền.",
    MEMBER_PLUS,
    MembersArgs,
)
async def query_members(svc: Services, ctx: UserContext, a: MembersArgs) -> ToolResult:
    snap = await svc.members.snapshot()
    labels = svc.config.sheets.members.role_labels
    members = snap.search(a.name, limit=5) if a.name else snap.active
    # member thường không thấy SĐT/MSSV người khác; Address không bao giờ trả ra
    show_private = ctx.role in STAFF
    out = []
    for m in members:
        item: dict[str, Any] = {
            "ho_ten": m.ho_ten,
            "vai_tro": labels.get(m.role, m.role),
            "email": m.email or None,
            "lop": m.lop or None,
            "truong": m.truong or None,
        }
        if show_private or (ctx.member is not None and ctx.member.key == m.key):
            item["sdt"] = m.sdt or None
            item["mssv"] = m.mssv or None
        out.append(item)
    if a.name and not out:
        return ToolResult({"members": [], "note": f"Không tìm thấy thành viên khớp '{a.name}'."})
    return ToolResult({"members": out, "total_active": len(snap.active)})


# ------------------------------------------------------------------ list_pending_applications


@REGISTRY.tool(
    "list_pending_applications",
    "Danh sách đơn xin gia nhập đang chờ duyệt (CHO_DUYET). Leader chỉ xem; duyệt/từ chối do admin.",
    LEADER_ADMIN,
)
async def list_pending_applications(svc: Services, ctx: UserContext, _: BaseModel) -> ToolResult:
    subs = await svc.submissions.list("don_xin_gia_nhap")
    pending = [s for s in subs if s.text("trang_thai") == "CHO_DUYET"]
    return ToolResult(
        {
            "applications": untrusted(
                [
                    {
                        "submission_id": s.text("submission_id"),
                        "ho_ten": s.text("ho_ten"),
                        "sdt": s.text("sdt"),
                        "email": s.text("email"),
                        "ngay_nop": fmt_date(s.submitted_on),
                        "link": s.text("link_file") or None,
                    }
                    for s in pending
                ]
            ),
            "count": len(pending),
        }
    )


# tool ghi (P3): form engine + yêu cầu chờ xác nhận
from app.tools import actions as _actions  # noqa: E402,F401
from app.tools import exports as _exports  # noqa: E402,F401
from app.tools import forms as _forms  # noqa: E402,F401
from app.tools import lab_ops as _lab_ops  # noqa: E402,F401
from app.tools import templates as _templates  # noqa: E402,F401
