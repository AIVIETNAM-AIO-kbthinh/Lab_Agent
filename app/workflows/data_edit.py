"""Sửa dữ liệu trong data/ qua yêu cầu chờ xác nhận (authentication_plan.md C6).

| target       | sheet / tab                       | xác định dòng                  | ai          | cột không sửa được            |
|--------------|-----------------------------------|--------------------------------|-------------|-------------------------------|
| device       | Equipment / Thietbi               | mã thiết bị (hoặc tên)         | leader/admin| Người mượn, Còn lại (tự tính) |
| borrow       | Equipment / LichSuMuon            | mã phiếu (+ thiết bị)          | leader/admin| Mã phiếu, Link phiếu          |
| duty         | DutySchedule / tab tuần           | họ tên + "Thứ 2 Sáng"...       | leader/admin| —                             |
| submission   | FormSubmissions / <form_id>       | submission_id                  | leader/admin| mã đơn, thời gian, người nộp, link; trạng thái đơn gia nhập |
| member       | Members (hoặc tab Cựu thành viên) | họ tên                         | chỉ admin   | never_expose (Address, No), Role (dùng đổi vai trò) |

AuditLog không sửa được qua bất kỳ đường nào. Mọi thay đổi ghi AuditLog kèm giá trị trước/sau.
Lúc xác nhận, worker đọc lại sheet: ô đã bị người khác sửa (khác giá trị lúc xem trước) thì dừng, không ghi đè.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.auth.roles import ADMIN_ONLY, LEADER_ADMIN, Role, UserContext
from app.domain.names import fuzzy_match, norm_name
from app.errors import PermissionDenied, ValidationFailed
from app.outbox.steps import OpKind, PreconditionFailed, StepContext, register
from app.sheets.a1 import col_letter, quote_tab
from app.sheets.table import cell_str, norm_header
from app.workflows.actions import finish, propose

if TYPE_CHECKING:
    from app.services import Services

TARGET_LABELS = {
    "device": "Thiết bị",
    "borrow": "Sổ mượn",
    "duty": "Lịch trực",
    "submission": "Đơn đã nộp",
    "member": "Thành viên",
}
APP_FORM = "don_xin_gia_nhap"
MAX_VALUE = 1000


@dataclass
class Sheet:
    """Một tab dạng bảng (dòng tiêu đề + các dòng dữ liệu) đọc nguyên văn để sửa theo TÊN CỘT trên sheet."""

    path: str
    tab: str
    header_row: int
    key_title: str
    protected: dict[str, str] = field(default_factory=dict)  # norm tiêu đề → lý do
    header: list[str] = field(default_factory=list)
    rows: list[tuple[int, list[str]]] = field(default_factory=list)  # (số dòng, ô)

    def col(self, title: str) -> int | None:
        k = norm_header(title)
        return next((i for i, h in enumerate(self.header) if norm_header(h) == k), None)

    def cell(self, cells: list[str], idx: int) -> str:
        return cells[idx] if idx < len(cells) else ""


async def _load(svc: Services, sh: Sheet) -> Sheet:
    sid = await svc.paths.file_id(sh.path)
    if not sh.tab:
        tabs = await svc.google.sheets.get_tabs(sid)
        sh.tab = tabs[0].title
    values = await svc.google.sheets.get_values(
        sid, f"{quote_tab(sh.tab)}!A{sh.header_row}:ZZ", render="FORMATTED_VALUE"
    )
    sh.header = [cell_str(h).strip() for h in (values[0] if values else [])]
    sh.rows = []
    for off, raw in enumerate(values[1:], start=1):
        cells = [cell_str(v) for v in raw]
        if any(c.strip() for c in cells):
            sh.rows.append((sh.header_row + off, cells))
    if sh.col(sh.key_title) is None:
        raise ValidationFailed(user_message=f"Tab '{sh.tab}' không có cột '{sh.key_title}'.")
    return sh


def _match_column(sh: Sheet, name: str) -> int:
    idx = sh.col(name)
    if idx is None:
        heads = [h for h in sh.header if h]
        hits = fuzzy_match(name, heads, limit=3, cutoff=80)
        if not hits or (len(hits) > 1 and hits[1][1] >= hits[0][1] - 3 and hits[0][1] < 100):
            raise ValidationFailed(
                user_message=f"Không rõ cột '{name}'. Các cột của tab {sh.tab}: {', '.join(heads)}."
            )
        idx = sh.col(hits[0][0])
        assert idx is not None
    reason = sh.protected.get(norm_header(sh.header[idx]))
    if reason:
        raise ValidationFailed(user_message=f"Không sửa được cột '{sh.header[idx]}': {reason}")
    return idx


def _pick_row(sh: Sheet, query: str, *, fuzzy: bool, label: str) -> tuple[int, list[str]]:
    key = sh.col(sh.key_title)
    assert key is not None
    exact = [r for r in sh.rows if norm_header(sh.cell(r[1], key)) == norm_header(query)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValidationFailed(user_message=f"Có {len(exact)} dòng {label} '{query}', hãy nói rõ hơn.")
    if fuzzy:
        names = [sh.cell(r[1], key) for r in sh.rows if sh.cell(r[1], key)]
        hits = fuzzy_match(query, names, limit=3, cutoff=80)
        if hits and (len(hits) == 1 or hits[0][1] == 100 or hits[1][1] < hits[0][1] - 5):
            return next(r for r in sh.rows if sh.cell(r[1], key) == hits[0][0])
        if hits:
            raise ValidationFailed(
                user_message=f"'{query}' khớp nhiều {label}: {', '.join(h for h, _ in hits)}. Hãy chọn một."
            )
    raise ValidationFailed(user_message=f"Không tìm thấy {label} '{query}' trong tab {sh.tab}.")


def _to_cell(v: str) -> str:
    v = (v or "").strip()[:MAX_VALUE]
    if re.fullmatch(r"\+?0\d{6,}", v):  # SĐT / mã có số 0 đầu: giữ nguyên dạng chữ
        return "'" + v
    return v


# ---------------------------------------------------------------- định nghĩa từng target


def _sheet_for(svc: Services, target: str, form_id: str | None, former: bool) -> Sheet:
    sc = svc.config.sheets
    if target == "device":
        d = sc.equipment.devices
        prot = {
            norm_header(d.columns[k]): "hệ thống tự tính sau mỗi lần mượn/trả"
            for k in ("nguoi_muon", "con_lai")
            if k in d.columns
        }
        return Sheet(sc.equipment.path, d.tab, d.header_row, d.columns["ma_tb"], prot)
    if target == "borrow":
        b = sc.equipment.borrows
        prot = {
            norm_header(b.columns[k]): "mã phiếu do hệ thống cấp"
            for k in ("ma_phieu", "link_phieu")
            if k in b.columns
        }
        return Sheet(sc.equipment.path, b.tab, b.header_row, b.columns["ma_phieu"], prot)
    if target == "submission":
        if not form_id:
            raise ValidationFailed(user_message="Cần form_id (loại đơn) để sửa đơn đã nộp.")
        prot = {
            "submission_id": "mã đơn do hệ thống cấp",
            "thoi_gian_nop": "thời điểm nộp không sửa được",
            "nguoi_nop": "người nộp không sửa được",
            "link_file": "file đã nộp không sửa qua chat",
        }
        if form_id == APP_FORM:
            prot["trang_thai"] = "duyệt / từ chối đơn gia nhập dùng thao tác duyệt đơn (chỉ admin)"
        return Sheet(sc.form_submissions.path, form_id, 1, "submission_id", prot)
    if target == "member":
        m = sc.members
        prot = {norm_header(c): "cột không bao giờ được sửa qua chatbot" for c in m.never_expose}
        prot[norm_header(m.columns["role"])] = "đổi vai trò dùng thao tác đổi vai trò (giữ đúng 1 leader)"
        tab = m.former_tab if former else (m.tab or "")
        return Sheet(m.path, tab, m.header_row, m.columns["ho_ten"], prot)
    raise ValidationFailed(user_message=f"Không sửa được loại dữ liệu '{target}'.")


def _check_role(ctx: UserContext, target: str) -> str:
    kind = "member_edit" if target == "member" else "data_edit"
    allowed = ADMIN_ONLY if kind == "member_edit" else LEADER_ADMIN
    if ctx.role not in allowed:
        who = "admin" if kind == "member_edit" else "leader hoặc admin"
        raise PermissionDenied(
            f"{ctx.role} không được sửa {target}", user_message=f"Chỉ {who} được sửa dữ liệu này."
        )
    return kind


# ---------------------------------------------------------------- tạo yêu cầu


async def propose_edit(
    svc: Services,
    ctx: UserContext,
    *,
    target: str,
    row: str,
    changes: list[dict[str, str]],
    form_id: str | None = None,
    thiet_bi: str | None = None,
    week: str | None = None,
    new_row: bool = False,
    former: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    kind = _check_role(ctx, target)
    if not changes:
        raise ValidationFailed(user_message="Chưa có thay đổi nào.")
    if target == "duty":
        return await _propose_duty(svc, ctx, row, changes, week)
    sh = await _load(svc, _sheet_for(svc, target, form_id, former))
    label = TARGET_LABELS[target].lower()
    if new_row:
        if target != "device":
            raise ValidationFailed(user_message="Chỉ thêm dòng mới được cho thiết bị.")
        key = sh.col(sh.key_title)
        assert key is not None
        if any(norm_header(sh.cell(c, key)) == norm_header(row) for _, c in sh.rows):
            raise ValidationFailed(user_message=f"Đã có thiết bị mã '{row}'.")
        added = [{"col": key, "column": sh.header[key], "before": "", "after": row.strip()}]
        for ch in changes:
            idx = _match_column(sh, ch["column"])
            if idx != key:
                added.append(
                    {"col": idx, "column": sh.header[idx], "before": "", "after": _to_cell(ch["value"])}
                )
        summary = [f"Thêm thiết bị mới vào tab {sh.tab}:"]
        summary += [f"• {c['column']}: {c['after'] or '(trống)'}" for c in added]
        payload = _payload(sh, target, None, row.strip(), added, [row.strip()])
        return await propose(svc, ctx, kind, payload, summary)

    rn, cells = (
        _pick_borrow_row(sh, row, thiet_bi)
        if target == "borrow"
        else _pick_row(sh, row, fuzzy=target in ("member", "device"), label=label)
    )
    key = sh.col(sh.key_title)
    assert key is not None
    out: list[dict[str, Any]] = []
    for ch in changes:
        idx = _match_column(sh, ch["column"])
        before = sh.cell(cells, idx)
        after = _to_cell(ch["value"])
        if before.strip() == after.lstrip("'"):
            continue
        out.append({"col": idx, "column": sh.header[idx], "before": before, "after": after})
    if not out:
        raise ValidationFailed(user_message="Giá trị mới giống hệt giá trị đang có, không cần sửa.")
    key_value = sh.cell(cells, key)
    summary = [f"{TARGET_LABELS[target]} · tab {sh.tab} · dòng {rn} ({key_value or row}):"]
    summary += [
        f"• {c['column']}: {c['before'] or '(trống)'} → {c['after'].lstrip(chr(39)) or '(trống)'}"
        for c in out
    ]
    if target == "member" and any(norm_header(c["column"]) == norm_header(sh.key_title) for c in out):
        summary.append(
            "Lưu ý: họ tên là khoá liên kết với lịch trực và sổ mượn; các dòng cũ ở đó không tự đổi theo."
        )
    refresh: list[str] = []
    if target == "device":
        refresh = [key_value]
    elif target == "borrow":
        ma_col = sh.col(svc.config.sheets.equipment.borrows.columns.get("ma_tb", ""))
        if ma_col is not None and sh.cell(cells, ma_col):
            refresh = [sh.cell(cells, ma_col)]
    payload = _payload(sh, target, rn, key_value, out, refresh)
    return await propose(svc, ctx, kind, payload, summary)


def _pick_borrow_row(sh: Sheet, ma_phieu: str, thiet_bi: str | None) -> tuple[int, list[str]]:
    key = sh.col(sh.key_title)
    assert key is not None
    rows = [r for r in sh.rows if norm_header(sh.cell(r[1], key)) == norm_header(ma_phieu)]
    if not rows:
        raise ValidationFailed(user_message=f"Không tìm thấy phiếu '{ma_phieu}' trong sổ mượn.")
    if len(rows) > 1:
        if not thiet_bi:
            raise ValidationFailed(
                user_message=f"Phiếu {ma_phieu} có {len(rows)} thiết bị, hãy nói rõ thiết bị nào (thiet_bi)."
            )
        q = norm_name(thiet_bi)
        rows = [r for r in rows if any(q and q in norm_name(c) for c in r[1])]
        if len(rows) != 1:
            raise ValidationFailed(
                user_message=f"Không xác định được đúng một dòng '{thiet_bi}' trong phiếu {ma_phieu}."
            )
    return rows[0]


def _payload(
    sh: Sheet, target: str, rn: int | None, key_value: str, changes: list[dict[str, Any]], refresh: list[str]
) -> dict[str, Any]:
    return {
        "target": target,
        "path": sh.path,
        "tab": sh.tab,
        "header_row": sh.header_row,
        "key_title": sh.key_title,
        "row_number": rn,
        "key_value": key_value,
        "changes": changes,
        "refresh_ma_tb": [x for x in refresh if x],
        "width": len(sh.header),
    }


# ---------------------------------------------------------------- lịch trực (ô theo buổi)


async def _propose_duty(
    svc: Services, ctx: UserContext, ho_ten: str, changes: list[dict[str, str]], week: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.workflows.duty import _find_slot, _open_tab

    tab, _ = await _open_tab(svc, week)
    hits = fuzzy_match(ho_ten, [r.name for r in tab.rows], limit=3, cutoff=80)
    if not hits:
        raise ValidationFailed(user_message=f"Không thấy '{ho_ten}' trong lịch trực tab {tab.title}.")
    if len(hits) > 1 and hits[0][1] < 100 and hits[1][1] >= hits[0][1] - 5:
        raise ValidationFailed(user_message=f"'{ho_ten}' khớp nhiều người: {', '.join(h for h, _ in hits)}.")
    row = tab.row_by_name(hits[0][0])
    assert row is not None
    cfg = svc.config.sheets.duty
    out: list[dict[str, Any]] = []
    for ch in changes:
        col_name = ch["column"].strip()
        m = re.match(r"^(.*?)\s+(\S+)$", col_name)
        slot = _find_slot(tab, m.group(1), m.group(2)) if m else None
        if slot is None:
            if tab.worklog_col and norm_header(col_name) in (
                norm_header(cfg.worklog_header),
                "noi dung cong viec",
            ):
                col, label = tab.worklog_col, cfg.worklog_header
            else:
                raise ValidationFailed(
                    user_message=f"Không rõ buổi '{col_name}'. Ghi dạng 'Thứ 2 Sáng', 'Chủ nhật Chiều'."
                )
        else:
            col, label = slot.col, tab.slot_text(slot)
        before = cell_str(row.marks.get(col)).strip() if slot else ""
        after = (ch["value"] or "").strip()[: (10 if slot else MAX_VALUE)]
        if slot and before.casefold() == after.casefold():
            continue
        out.append(
            {"col": col - 1, "column": label, "before": before, "after": after, "slot": slot is not None}
        )
    if not out:
        raise ValidationFailed(user_message="Giá trị mới giống hệt giá trị đang có, không cần sửa.")
    summary = [f"Lịch trực tab {tab.title} · {row.name}:"]
    summary += [f"• {c['column']}: {c['before'] or '(trống)'} → {c['after'] or '(trống)'}" for c in out]
    payload = {
        "target": "duty",
        "path": cfg.path,
        "tab": tab.title,
        "row_number": row.row_number,
        "key_value": row.name,
        "changes": out,
        "refresh_ma_tb": [],
        "duty_slots": True,
    }
    return await propose(svc, ctx, "data_edit", payload, summary)


# ---------------------------------------------------------------- worker


async def _precheck(ctx: StepContext) -> dict[str, Any] | None:
    p = ctx.payload
    if p.get("duty_slots"):
        props = next((t for t in await ctx.svc.duty.tabs() if t.title.strip() == p["tab"]), None)
        if props is None:
            raise PreconditionFailed(f"Tab lịch trực {p['tab']} không còn.")
        tab = await ctx.svc.duty.load_tab(props)
        row = next((r for r in tab.rows if r.row_number == p["row_number"]), None)
        if row is None or norm_name(row.name) != norm_name(p["key_value"]):
            raise PreconditionFailed("Lịch trực vừa bị sắp xếp lại, hãy tạo yêu cầu mới.")
        for c in p["changes"]:
            if not c.get("slot", True):
                continue
            cur = cell_str(row.marks.get(c["col"] + 1)).strip()
            if cur.casefold() not in (c["before"].casefold(), c["after"].casefold()):
                raise PreconditionFailed(f"Ô {c['column']} vừa bị sửa thành '{cur}', hãy kiểm tra lại.")
        return None
    sh = await _load(ctx.svc, Sheet(p["path"], p["tab"], p["header_row"], p["key_title"]))
    key = sh.col(p["key_title"])
    assert key is not None
    if p["row_number"] is None:
        if any(norm_header(sh.cell(c, key)) == norm_header(p["key_value"]) for _, c in sh.rows):
            raise PreconditionFailed(f"Đã có dòng '{p['key_value']}', không thêm trùng.")
        return None
    cells = next((c for rn, c in sh.rows if rn == p["row_number"]), None)
    if cells is None or norm_header(sh.cell(cells, key)) != norm_header(p["key_value"]):
        raise PreconditionFailed("Dòng cần sửa đã bị di chuyển hoặc xoá trên sheet, hãy tạo yêu cầu mới.")
    for c in p["changes"]:
        cur = sh.cell(cells, c["col"]).strip()
        if cur not in (c["before"].strip(), c["after"].lstrip("'")):
            raise PreconditionFailed(f"Ô '{c['column']}' vừa bị sửa thành '{cur}', hãy kiểm tra lại.")
    return None


async def step_write(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    sheets = ctx.svc.google.sheets
    sid = await ctx.svc.paths.file_id(p["path"])
    tab = quote_tab(p["tab"])
    if p["row_number"] is None:  # thêm thiết bị mới
        row: list[str] = [""] * max(p.get("width") or 0, max(c["col"] for c in p["changes"]) + 1)
        for c in p["changes"]:
            row[c["col"]] = c["after"]
        await sheets.append_values(sid, f"{tab}!A{p['header_row']}", [row])
        return {"appended": True}
    await sheets.batch_update_values(
        sid, [(f"{tab}!{col_letter(c['col'] + 1)}{p['row_number']}", [[c["after"]]]) for c in p["changes"]]
    )
    return {"cells": len(p["changes"])}


async def step_refresh(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    if p["target"] in ("device", "borrow") and p["refresh_ma_tb"]:
        await ctx.svc.equipment.refresh_nguoi_muon(p["refresh_ma_tb"])
    if p["target"] == "member":
        ctx.svc.members.invalidate()
    return {}


async def step_audit(ctx: StepContext) -> None:
    p = ctx.payload
    await ctx.svc.audit.log(
        ctx.actor,
        p.get("requested_role", Role.LEADER.value),
        "member_edit" if p["target"] == "member" else "data_edit",
        f"{TARGET_LABELS[p['target']]} · {p['tab']} · {p['key_value']}",
        True,
        {"changes": [{k: c[k] for k in ("column", "before", "after")} for c in p["changes"]]},
    )


async def step_finish(ctx: StepContext) -> dict[str, Any]:
    p = ctx.payload
    what = ", ".join(c["column"] for c in p["changes"])
    verb = "Đã thêm" if p["row_number"] is None else "Đã cập nhật"
    return await finish(
        ctx.svc, p["action_id"], f"{verb} {TARGET_LABELS[p['target']].lower()} '{p['key_value']}': {what}."
    )


for _kind in ("data_edit", "member_edit"):
    register(
        OpKind(
            f"action:{_kind}",
            [
                ("write", step_write),
                ("refresh", step_refresh),
                ("audit", step_audit),
                ("finish", step_finish),
            ],
            precheck=_precheck,
        )
    )
