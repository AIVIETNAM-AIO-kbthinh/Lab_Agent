"""Tool xuất file theo yêu cầu: bảng lọc từ dữ liệu lab (Excel/CSV) hoặc các đơn đã nộp (DOCX, nhiều đơn → ZIP).

Dữ liệu trong file KHÔNG trả cho LLM; LLM chỉ nhận số dòng/số đơn, tên file, bộ lọc đã áp dụng.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.auth.roles import MEMBER_PLUS, UserContext
from app.domain.dates import fmt_date
from app.exports import store
from app.exports.build import ExportError, ExportRequest, build_table
from app.exports.forms import FormExportRequest, export_forms
from app.logging import get_logger
from app.tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


@REGISTRY.tool(
    "export_data",
    "Xuất FILE (Excel hoặc CSV) để người dùng tải về, lọc từ dữ liệu lab: danh sách thành viên, danh sách "
    "thiết bị, thiết bị đang mượn / quá hạn / lịch sử mượn, lịch trực theo tuần. Dùng khi người dùng muốn "
    "'xuất', 'tải', 'gửi file', 'file excel'... Chỉ hỏi để tra thông tin thì dùng query_members / "
    "query_equipment / query_duty.",
    MEMBER_PLUS,
    ExportRequest,
    timeout_s=60,
)
async def export_data(svc: Services, ctx: UserContext, a: ExportRequest) -> ToolResult:
    try:
        table = await build_table(svc, ctx, a)
    except ExportError as e:
        return ToolResult({"error": str(e), **e.details}, is_error=True)
    base = {
        "so_dong": len(table.rows),
        "bo_loc": table.filters,
        **({"luu_y": table.notes} if table.notes else {}),
    }
    if not table.rows:
        return ToolResult({**base, "note": "Không có dòng nào khớp bộ lọc nên không tạo file."})
    limit = svc.config.exports.max_rows
    if len(table.rows) > limit:
        return ToolResult(
            {
                **base,
                "note": f"Quá {limit} dòng, hãy nhờ người dùng lọc hẹp hơn (theo thời gian, trạng thái...).",
            }
        )
    saved = await store.save(svc, ctx, table, a.format)
    log.info("export_created", dataset=table.dataset, rows=len(table.rows), fmt=a.format, actor=ctx.actor)
    ttl = svc.config.exports.ttl_minutes
    return ToolResult(
        {
            **base,
            "file": saved.filename,
            "cot": [c.label for c in table.columns],
            "note": f"Hệ thống đã hiện nút tải file cho người dùng; link dùng được trong {ttl} phút.",
        },
        ui_actions=[
            {
                "type": "download",
                "url": f"/exports/{saved.id}",
                "filename": saved.filename,
                "title": f"Tải {saved.filename} ({len(table.rows)} dòng)",
            }
        ],
    )


@REGISTRY.tool(
    "export_form_files",
    "Xuất FILE các ĐƠN ĐÃ NỘP để người dùng tải về, đúng định dạng đang lưu trên Drive (phiếu mượn thiết bị, "
    "đơn gia nhập thường là PDF; biên bản họp là Word): lọc theo người nộp, thời gian nộp, mã phiếu. Một đơn → "
    "1 file; nhiều đơn → file zip. Dùng khi người dùng muốn file/bản gốc của các đơn, KHÔNG phải bảng tổng hợp "
    "(bảng → export_data).",
    MEMBER_PLUS,
    FormExportRequest,
    timeout_s=180,
)
async def export_form_files(svc: Services, ctx: UserContext, a: FormExportRequest) -> ToolResult:
    try:
        res = await export_forms(svc, ctx, a)
    except ExportError as e:
        return ToolResult({"error": str(e), **e.details}, is_error=True)
    base: dict[str, Any] = {
        "so_don": len(res.files),
        "bo_loc": res.filters,
        **({"luu_y": res.notes} if res.notes else {}),
    }
    if not res.files:
        note = (
            "Có đơn khớp nhưng không còn file trên Drive nên không tạo file; báo người dùng liên hệ thư ký."
            if res.missing
            else "Không có đơn nào khớp bộ lọc nên không tạo file."
        )
        return ToolResult({**base, "note": note})
    saved = await store.save_bytes(
        svc,
        ctx,
        f"forms:{res.schema.form_id}",
        res.filename,
        res.mime,
        res.data,
        len(res.files),
        res.filters,
    )
    log.info("export_forms_created", form_id=res.schema.form_id, files=len(res.files), actor=ctx.actor)
    ttl = svc.config.exports.ttl_minutes
    return ToolResult(
        {
            **base,
            "file": saved.filename,
            "cac_don": [
                {
                    "ten_file": f.name,
                    "ngay_nop": fmt_date(f.submission.submitted_on),
                    "nguoi_nop": f.submission.text("nguoi_nop"),
                }
                for f in res.files
            ],
            "note": f"Hệ thống đã hiện nút tải file cho người dùng; link dùng được trong {ttl} phút.",
        },
        ui_actions=[
            {
                "type": "download",
                "url": f"/exports/{saved.id}",
                "filename": saved.filename,
                "title": f"Tải {saved.filename} ({len(res.files)} đơn)",
            }
        ],
    )
