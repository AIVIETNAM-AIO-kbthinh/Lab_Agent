"""Tool ghi mới (authentication_plan.md đợt 2): lịch trực của mình (C9), đẩy tài liệu vào KB (C4), sửa dữ liệu
trong data/ (C6). Tool chỉ TẠO yêu cầu + bản tóm tắt + nút Xác nhận; việc ghi chỉ xảy ra khi người dùng bấm nút.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from app.auth.roles import LEADER_ADMIN, MEMBER_PLUS, STAFF, UserContext
from app.tools.registry import REGISTRY, ToolResult
from app.workflows import data_edit as wf_data
from app.workflows import duty as wf_duty
from app.workflows import kb as wf_kb

if TYPE_CHECKING:
    from app.services import Services


# ------------------------------------------------------------------ register_duty (C9)


class DutySlotIn(BaseModel):
    thu: str = Field(description="Thứ: 'Thứ 2' … 'Thứ 7', 'Chủ nhật'.", max_length=20)
    buoi: Literal["Sáng", "Chiều"] = Field(description="Buổi trong ngày.")


class RegisterDutyArgs(BaseModel):
    action: Literal["register", "unregister"] = Field(
        description="register = đăng ký trực các buổi; unregister = bỏ đăng ký (chỉ buổi tự đăng ký 'V')."
    )
    slots: list[DutySlotIn] = Field(min_length=1, max_length=14)
    week: str | None = Field(
        None,
        description="Tuần, NGUYÊN VĂN người dùng ('tuần này', 'tuần sau', một ngày dd/mm). Bỏ trống = tuần "
        "đang mở đăng ký (tuần sau nếu đã có lịch, không thì tuần này).",
        max_length=60,
    )


@REGISTRY.tool(
    "register_duty",
    "Đăng ký / bỏ đăng ký buổi trực lab của CHÍNH người hỏi (không sửa được cho người khác). Hiện bản tóm tắt "
    "kèm nút Xác nhận; chỉ khi người dùng bấm nút mới ghi vào lịch trực.",
    MEMBER_PLUS,
    RegisterDutyArgs,
)
async def register_duty(svc: Services, ctx: UserContext, a: RegisterDutyArgs) -> ToolResult:
    data, ui = await wf_duty.propose_register(svc, ctx, a.action, [s.model_dump() for s in a.slots], a.week)
    return ToolResult(data, ui_actions=[ui])


# ------------------------------------------------------------------ publish_kb_document (C4)


class PublishKbArgs(BaseModel):
    upload_id: str = Field(description="upload_id của file người dùng vừa gửi (pdf/docx).", max_length=32)
    folder: Literal["public", "internal"] = Field(
        description="public = tài liệu công khai (nội quy, giới thiệu; khách cũng đọc được); internal = tài "
        "liệu nội bộ (chỉ thành viên). HỎI người dùng nếu họ chưa nói rõ."
    )
    title: str | None = Field(
        None, description="Tên file muốn lưu (không cần đuôi), nếu người dùng nói.", max_length=120
    )
    subfolder: str | None = Field(
        None,
        description="Thư mục con trong kb/public hoặc kb/internal, CHỈ khi người dùng nói (vd 'noi-quy').",
        max_length=60,
    )


@REGISTRY.tool(
    "publish_kb_document",
    "(Thư ký/leader/admin) Đưa file người dùng vừa gửi vào kho kiến thức của lab (kb/public hoặc kb/internal) "
    "để trợ lý dùng trả lời: nội quy mới, tài liệu, thông báo... Hiện bản tóm tắt + nút Xác nhận; không cần "
    "duyệt thêm. KHÔNG dùng cho đơn tự điền hay mẫu đơn mới.",
    STAFF,
    PublishKbArgs,
)
async def publish_kb_document(svc: Services, ctx: UserContext, a: PublishKbArgs) -> ToolResult:
    data, ui = await wf_kb.propose_publish(svc, ctx, a.upload_id, a.folder, a.title, a.subfolder)
    return ToolResult(data, ui_actions=[ui])


# ------------------------------------------------------------------ request_data_edit (C6)


class ChangeIn(BaseModel):
    column: str = Field(
        description="Tên cột đúng như trên sheet (vd 'Trạng thái hoạt động', 'Số lượng', 'Hạn trả', 'Phone'). "
        "Với lịch trực: buổi dạng 'Thứ 2 Sáng'.",
        max_length=80,
    )
    value: str = Field(description="Giá trị mới; chuỗi rỗng = xoá ô. Ngày dạng dd/mm/yyyy.", max_length=1000)


class DataEditArgs(BaseModel):
    target: Literal["device", "borrow", "duty", "submission", "member"] = Field(
        description="device = thiết bị (tab Thietbi); borrow = dòng sổ mượn (LichSuMuon); duty = ô lịch trực; "
        "submission = dòng đơn đã nộp (FormSubmissions); member = thông tin thành viên (chỉ admin)."
    )
    row: str = Field(
        description="Xác định dòng: device → mã thiết bị (hoặc tên); borrow → mã phiếu; duty / member → họ tên; "
        "submission → submission_id.",
        max_length=200,
    )
    changes: list[ChangeIn] = Field(min_length=1, max_length=20)
    form_id: str | None = Field(
        None, description="[submission] Mã loại đơn, vd 'phieu_muon_thiet_bi'.", max_length=100
    )
    thiet_bi: str | None = Field(
        None, description="[borrow] Thiết bị trong phiếu khi phiếu có nhiều dòng.", max_length=200
    )
    week: str | None = Field(
        None, description="[duty] Tuần, NGUYÊN VĂN người dùng. Bỏ trống = tuần đang mở.", max_length=60
    )
    new_row: bool = Field(False, description="[device] true = thêm thiết bị MỚI với mã ở `row`.")
    former: bool = Field(False, description="[member] true = sửa ở tab Cựu thành viên.")


@REGISTRY.tool(
    "request_data_edit",
    "(Leader/admin) Tạo yêu cầu sửa dữ liệu của lab: thông tin/tình trạng thiết bị hoặc thêm thiết bị mới, dòng "
    "sổ mượn, ô lịch trực của bất kỳ ai, dòng đơn đã nộp; admin còn sửa được thông tin thành viên. Hiện bảng "
    "trước/sau + nút Xác nhận. Không sửa được AuditLog, vai trò (dùng request_member_change) hay trạng thái đơn "
    "gia nhập.",
    LEADER_ADMIN,
    DataEditArgs,
)
async def request_data_edit(svc: Services, ctx: UserContext, a: DataEditArgs) -> ToolResult:
    data, ui = await wf_data.propose_edit(
        svc,
        ctx,
        target=a.target,
        row=a.row,
        changes=[c.model_dump() for c in a.changes],
        form_id=a.form_id,
        thiet_bi=a.thiet_bi,
        week=a.week,
        new_row=a.new_row,
        former=a.former,
    )
    return ToolResult(data, ui_actions=[ui])
