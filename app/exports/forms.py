"""Xuất các đơn đã nộp (phiếu mượn thiết bị, biên bản họp, đơn gia nhập...) để tải về, ĐÚNG file đang lưu.

Tìm đơn trên FormSubmissions/<form_id> theo người, ngày nộp, mã phiếu, rồi tải nguyên file đơn trên Drive
(PDF là PDF, DOCX là DOCX; Google Doc không có file gốc nên xuất ra DOCX). Không dựng lại file từ dữ liệu.
Chỉ nhận file Drive có appProperties `labagent_submission` đúng mã đơn (file do hệ thống tải lên), không tải
link tuỳ ý trong sheet; đơn không còn file trên Drive thì bỏ qua và báo lại. Một đơn → một file; nhiều đơn → ZIP.

Quyền: ai cũng xuất được đơn của chính mình; biên bản họp: mọi thành viên; đơn của người khác, kể cả đơn gia nhập:
thư ký/leader/admin.
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app import clock
from app.auth.roles import STAFF, UserContext
from app.domain.dates import fmt_date, resolve_time_expr
from app.domain.names import norm_name, to_pascal_ascii
from app.domain.submissions import Submission
from app.exports.build import ExportError, known_people, pick_name
from app.forms.registry import FormDef
from app.forms.schema import FormSchema
from app.google.drive import DOCX, GDOC, PDF, DriveFile
from app.logging import get_logger

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)
ZIP = "application/zip"
_DRIVE_ID = re.compile(
    r"/(?:file/d|document/d|drive/folders|folders)/([A-Za-z0-9_-]+)|[?&]id=([A-Za-z0-9_-]+)"
)


class FormExportRequest(BaseModel):
    form_id: str = Field(
        description="Mã loại đơn: phieu_muon_thiet_bi (phiếu mượn thiết bị), bien_ban_hop (biên bản họp), "
        "don_xin_gia_nhap (đơn xin gia nhập); đơn khác lấy form_id từ list_forms.",
        max_length=100,
    )
    person: str | None = Field(
        None,
        description="Họ tên người nộp / người đứng tên đơn (khớp gần đúng). Người dùng nói 'của tôi' thì truyền "
        "họ tên của chính họ. Bỏ trống = mọi người (nếu có quyền).",
        max_length=200,
    )
    time_expr: str | None = Field(
        None,
        description="Lọc theo NGÀY NỘP, truyền NGUYÊN VĂN biểu thức thời gian, vd 'tuần trước', 'tháng 9'.",
        max_length=100,
    )
    code: str | None = Field(
        None,
        description="Mã phiếu (vd 'P-20261016-01') hoặc submission_id nếu người dùng nêu.",
        max_length=60,
    )


@dataclass
class FormFile:
    submission: Submission
    name: str
    data: bytes
    mime: str


@dataclass
class FormExport:
    schema: FormSchema
    files: list[FormFile]
    filters: dict[str, str]
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # mã đơn không còn file trên Drive
    filename: str = ""
    mime: str = ZIP
    data: bytes = b""


# ------------------------------------------------------------------ quyền + lọc


def _owner_name(schema: FormSchema, s: Submission) -> str:
    """Người đứng tên đơn: trường ho_ten nếu đơn có, không thì cột nguoi_nop."""
    if any(f.key == "ho_ten" for f in schema.fields) and s.text("ho_ten"):
        return s.text("ho_ten")
    return s.text("nguoi_nop")


def _is_own(schema: FormSchema, s: Submission, ctx: UserContext) -> bool:
    if not ctx.ho_ten:
        return False
    me = norm_name(ctx.ho_ten)
    return norm_name(_owner_name(schema, s)) == me or norm_name(s.text("nguoi_nop")) == me


def _sees_all(schema: FormSchema, ctx: UserContext) -> bool:
    """Thư ký / leader / admin: mọi đơn, kể cả đơn gia nhập (authentication_plan.md C3).
    Member: biên bản họp (tài liệu nội bộ vốn đọc được) và đơn của chính mình."""
    if ctx.role in STAFF:
        return True
    return schema.hook == "meeting_minutes"


def _code_of(s: Submission) -> str:
    m = re.search(r"Mã phiếu\s+(\S+)", s.text("ghi_chu"))
    return m.group(1) if m else ""


async def _active_form(svc: Services, form_id: str) -> FormDef:
    fd = await svc.forms.active(form_id.strip())
    if fd is None:
        forms = await svc.forms.list_active()
        raise ExportError(
            f"Không có loại đơn '{form_id}'.", valid_form_ids={d.form_id: d.schema.title for d in forms}
        )
    return fd


async def find_submissions(
    svc: Services, ctx: UserContext, req: FormExportRequest
) -> tuple[FormDef, list[Submission], dict[str, str], list[str]]:
    fd = await _active_form(svc, req.form_id)
    schema = fd.schema
    notes: list[str] = []
    filters: dict[str, str] = {"Loại đơn": schema.title}
    subs = await svc.submissions.list(schema.form_id)
    sees_all = _sees_all(schema, ctx)
    if not sees_all:
        if "guest" in schema.roles:
            raise ExportError(f"Chỉ thư ký, leader hoặc admin được xuất {schema.title.lower()}.")
        if not ctx.ho_ten:
            raise ExportError(
                "Tài khoản của bạn không gắn với thành viên nào nên không xuất được đơn đã nộp."
            )

    if req.person:
        names = sorted({_owner_name(schema, s) for s in subs if _owner_name(schema, s)})
        if not names:
            raise ExportError(f"Chưa có {schema.title.lower()} nào được nộp.")
        who = pick_name(req.person, names, "người nộp", known=await known_people(svc), notes=notes)
        if not sees_all and norm_name(who) != norm_name(ctx.ho_ten or ""):
            raise ExportError(f"Bạn chỉ xuất được {schema.title.lower()} của chính mình.")
        subs = [s for s in subs if norm_name(_owner_name(schema, s)) == norm_name(who)]
        filters["Người nộp"] = who
    elif not sees_all:
        subs = [s for s in subs if _is_own(schema, s, ctx)]
        filters["Người nộp"] = ctx.ho_ten or ""
        notes.append(f"Bạn chỉ xuất được {schema.title.lower()} của chính mình nên file chỉ gồm đơn của bạn.")

    if req.time_expr:
        rng = resolve_time_expr(req.time_expr, clock.today())
        if rng is None:
            raise ExportError(f"Không hiểu biểu thức thời gian '{req.time_expr}'.")
        start, end = rng
        subs = [s for s in subs if s.submitted_on is not None and start <= s.submitted_on <= end]
        filters["Ngày nộp"] = f"{fmt_date(start)} – {fmt_date(end)}"
    if req.code:
        c = req.code.strip().casefold()
        subs = [s for s in subs if c in (s.text("submission_id").casefold(), _code_of(s).casefold())]
        filters["Mã"] = req.code.strip()
    subs.sort(key=lambda s: (s.submitted_on or date.min, s.row_number))
    return fd, subs, filters, notes


# ------------------------------------------------------------------ lấy file


async def _drive_main(svc: Services, s: Submission) -> DriveFile | None:
    """File đơn trên Drive do hệ thống tải lên (khớp appProperties), hoặc None."""
    sid = s.text("submission_id")
    m = _DRIVE_ID.search(s.text("link_file"))
    if not sid or not m:
        return None
    try:
        f = await svc.google.drive.get_file(m.group(1) or m.group(2))
        if f.is_folder:  # đơn gia nhập lưu link folder hồ sơ
            kids = await svc.google.drive.list_children(f.id)
            return next(
                (
                    k
                    for k in kids
                    if k.app_properties.get("labagent_submission") == sid
                    and k.app_properties.get("labagent_role") == "main"
                ),
                None,
            )
    except Exception as e:  # file bị xoá / không có quyền
        log.warning("export_drive_lookup_failed", submission_id=sid, error=str(e)[:200])
        return None
    if f.trashed or f.app_properties.get("labagent_submission") != sid:
        return None
    return f


_EXT = {PDF: "pdf", DOCX: "docx"}


async def _file_for(svc: Services, s: Submission) -> FormFile | None:
    drive = svc.google.drive
    f = await _drive_main(svc, s)
    if f is None:
        return None
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", f.name) or s.text("submission_id")
    if f.mime_type == GDOC:  # Google Doc không có file nhị phân gốc: Drive chỉ cho tải dưới dạng xuất
        return FormFile(s, f"{base}.docx", await drive.export(f.id, DOCX), DOCX)
    ext = _EXT.get(f.mime_type) or (f.name.rpartition(".")[2] if "." in f.name else "bin")
    return FormFile(s, f"{base}.{ext}", await drive.download(f.id), f.mime_type)


def _zip(files: list[FormFile]) -> bytes:
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            name, n = f.name, 2
            while name.casefold() in used:
                stem, dot, ext = f.name.rpartition(".")
                name, n = f"{stem}_{n}{dot}{ext}", n + 1
            used.add(name.casefold())
            z.writestr(name, f.data)
    return buf.getvalue()


async def export_forms(svc: Services, ctx: UserContext, req: FormExportRequest) -> FormExport:
    fd, subs, filters, notes = await find_submissions(svc, ctx, req)
    out = FormExport(fd.schema, [], filters, notes)
    if not subs:
        return out
    limit = svc.config.exports.max_files
    if len(subs) > limit:
        raise ExportError(
            f"Có {len(subs)} đơn khớp, quá giới hạn {limit} đơn mỗi lần xuất. Hãy lọc hẹp hơn "
            "(theo người nộp, thời gian, mã phiếu).",
            so_don=len(subs),
        )
    sem = asyncio.Semaphore(4)

    async def one(s: Submission) -> FormFile | None:
        async with sem:
            return await _file_for(svc, s)

    got = await asyncio.gather(*(one(s) for s in subs))
    out.files = [f for f in got if f is not None]
    out.missing = [
        _code_of(s) or s.text("submission_id") for s, f in zip(subs, got, strict=True) if f is None
    ]
    if out.missing:
        notes.append(
            f"{len(out.missing)} đơn không còn file trên Drive (có thể đã bị xoá/di chuyển) nên không có trong "
            f"file xuất: {', '.join(out.missing)}."
        )
    if not out.files:
        return out
    if len(out.files) == 1:
        f = out.files[0]
        out.filename, out.data, out.mime = f.name, f.data, f.mime
    else:
        parts = [to_pascal_ascii(fd.schema.title)]
        if "Người nộp" in filters:
            parts.append(to_pascal_ascii(filters["Người nộp"]))
        parts.append(fmt_date(clock.today(), "%d-%m-%Y"))
        out.filename, out.data, out.mime = "_".join(p for p in parts if p) + ".zip", _zip(out.files), ZIP
    return out
