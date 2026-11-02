"""Tool form engine (TK §5). Quyền theo `roles` của từng form; tool trên bản nháp kiểm tra chủ bản nháp (session).

KHÔNG có tool xác nhận: nút Xác nhận/Huỷ gọi thẳng endpoint backend (TK §7.5).
Giá trị trong bản nháp có thể đến từ file người dùng ⇒ trả cho LLM trong `untrusted_content`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.auth.roles import Role, UserContext
from app.forms import submit as _submit  # noqa: F401  (đăng ký op outbox form_submit:*)
from app.forms.engine import EngineResult, FormEngine
from app.forms.extract import FieldIn
from app.tools.registry import REGISTRY, ToolResult, untrusted

if TYPE_CHECKING:
    from app.services import Services

ALL = {Role.GUEST, Role.MEMBER, Role.THU_KI, Role.LEADER, Role.ADMIN}
FORM_ID = Field(description="form_id lấy từ list_forms, vd 'phieu_muon_thiet_bi'.", max_length=100)
DRAFT_ID = Field(description="draft_id do start_form / submit_filled_form trả về.", max_length=32)
UPLOAD_ID = Field(
    description="upload_id của file người dùng đã tải lên (ghi trong tin nhắn của họ).", max_length=32
)


def _result(r: EngineResult) -> ToolResult:
    data: dict[str, Any] = dict(r.data)
    for k in ("filled", "summary", "normalized"):
        if k in data:
            data[k] = untrusted(data[k])
    return ToolResult(data, ui_actions=r.ui_actions)


class FormArgs(BaseModel):
    form_id: str = FORM_ID


class StartArgs(BaseModel):
    form_id: str = FORM_ID
    fields: list[FieldIn] | None = Field(
        None,
        description="MỌI thông tin người dùng đã nói trong hội thoại cho đơn này (cùng định dạng với "
        "update_form_fields; key là key trường, vd 'muc_dich', 'han_tra', 'thiet_bi'). Bỏ trống nếu chưa có.",
        max_length=40,
    )


class DraftArgs(BaseModel):
    draft_id: str = DRAFT_ID


class UpdateArgs(BaseModel):
    draft_id: str = DRAFT_ID
    fields: list[FieldIn] = Field(
        description="Các trường người dùng vừa cung cấp, `key` lấy đúng từ `form_fields` (vd 'ho_ten'). "
        "Mỗi thông tin vào ĐÚNG trường của nó, không gộp nhiều thông tin vào một trường. Trường đơn dùng `value`; trường nhiều tên dùng `items`; "
        "trường bảng dùng `rows` (gửi lại TOÀN BỘ bảng). Chỉ gửi đúng chữ người dùng nói, không suy đoán. "
        "Để xoá một giá trị: gửi key với value null.",
        max_length=40,
    )


class AttachArgs(BaseModel):
    draft_id: str = DRAFT_ID
    key: str = Field(description="Key trường đính kèm, vd 'cv'.", max_length=60)
    upload_id: str = UPLOAD_ID


class SubmitFilledArgs(BaseModel):
    form_id: str = FORM_ID
    upload_id: str = UPLOAD_ID


@REGISTRY.tool(
    "get_form_template",
    "Gửi mẫu đơn trống cho người dùng tải về tự điền, kèm lời mời điền hộ qua chat. Dùng khi người dùng muốn "
    "nộp một loại đơn.",
    ALL,
    FormArgs,
)
async def get_form_template(svc: Services, ctx: UserContext, a: FormArgs) -> ToolResult:
    return _result(await FormEngine(svc).blank_info(a.form_id, ctx))


@REGISTRY.tool(
    "start_form",
    "Bắt đầu (hoặc tiếp tục) điền hộ một đơn qua chat VÀ điền luôn các thông tin người dùng đã nói (tham số "
    "`fields`). Trả draft_id, danh sách trường (form_fields), trường còn thiếu/sai.",
    ALL,
    StartArgs,
)
async def start_form(svc: Services, ctx: UserContext, a: StartArgs) -> ToolResult:
    eng = FormEngine(svc)
    res = await eng.start(a.form_id, ctx)
    if a.fields:
        res = await eng.update(res.data["draft_id"], a.fields, ctx)
    return _result(res)


@REGISTRY.tool(
    "update_form_fields",
    "Cập nhật các trường của bản nháp từ thông tin người dùng cung cấp. Trả danh sách còn thiếu/sai để hỏi lại "
    "GỘP trong một tin nhắn. Trường khoá (lấy từ tài khoản) không sửa được.",
    ALL,
    UpdateArgs,
)
async def update_form_fields(svc: Services, ctx: UserContext, a: UpdateArgs) -> ToolResult:
    return _result(await FormEngine(svc).update(a.draft_id, a.fields, ctx))


@REGISTRY.tool(
    "attach_file",
    "Gắn file người dùng đã tải lên (vd CV) vào trường đính kèm của bản nháp.",
    ALL,
    AttachArgs,
)
async def attach_file(svc: Services, ctx: UserContext, a: AttachArgs) -> ToolResult:
    return _result(await FormEngine(svc).attach(a.draft_id, a.key, a.upload_id, ctx))


@REGISTRY.tool(
    "submit_filled_form",
    "Người dùng gửi file đơn họ TỰ ĐIỀN: trích xuất, kiểm tra. Hợp lệ thì hiện nút xác nhận nộp; sai thì trả "
    "lỗi để hỏi lại.",
    ALL,
    SubmitFilledArgs,
)
async def submit_filled_form(svc: Services, ctx: UserContext, a: SubmitFilledArgs) -> ToolResult:
    return _result(await FormEngine(svc).submit_filled(a.form_id, a.upload_id, ctx))


@REGISTRY.tool(
    "render_preview",
    "Tạo bản xem trước (PDF) của đơn đã đủ thông tin và hiện nút Xác nhận/Huỷ cho người dùng. Gọi lại sau mỗi "
    "lần sửa. Đơn chưa hợp lệ thì trả về các lỗi hiện tại (dùng khi người dùng báo đơn bị lỗi).",
    ALL,
    DraftArgs,
)
async def render_preview(svc: Services, ctx: UserContext, a: DraftArgs) -> ToolResult:
    return _result(await FormEngine(svc).preview(a.draft_id, ctx))


@REGISTRY.tool("cancel_form", "Huỷ bản nháp đơn khi người dùng không muốn nộp nữa.", ALL, DraftArgs)
async def cancel_form(svc: Services, ctx: UserContext, a: DraftArgs) -> ToolResult:
    return _result(await FormEngine(svc).cancel(a.draft_id, ctx))
