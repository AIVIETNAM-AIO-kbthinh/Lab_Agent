"""Tool thêm mẫu đơn mới (P4) — leader và admin (authentication_plan.md C5).

Tool chỉ SINH và SỬA bản duyệt (thẻ xem trước + nút). KHÔNG có tool kích hoạt: chỉ khi admin bấm nút Kích hoạt
(endpoint backend) mẫu đơn mới được lưu lên Drive và mở cho người dùng (TK §1.3, §5).
Nội dung lấy từ file admin gửi (nhãn, thông tin mẫu) trả cho LLM trong `untrusted_content`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.auth.roles import LEADER_ADMIN, UserContext
from app.forms.engine import EngineResult
from app.forms.schema_gen import activate as _activate  # noqa: F401  (đăng ký op outbox template_activate)
from app.forms.schema_gen.review import FieldChange, RoleIn, TemplateReview
from app.tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from app.services import Services

TD_ID = Field(description="template_draft_id do propose_template trả về.", max_length=32)


def _result(r: EngineResult) -> ToolResult:
    return ToolResult(dict(r.data), ui_actions=r.ui_actions)


class ProposeArgs(BaseModel):
    upload_id: str = Field(description="upload_id của file mẫu đơn (.docx) admin vừa gửi.", max_length=32)
    title: str | None = Field(
        None, description="Tên đơn nếu admin đã nói; bỏ trống để tự nhận diện.", max_length=200
    )
    roles: list[RoleIn] | None = Field(
        None,
        description="Ai được dùng đơn, CHỈ khi admin đã nói rõ (guest = người ngoài lab; member/thu_ki/leader). "
        "Bỏ trống để hệ thống đề xuất.",
    )
    form_id: str | None = Field(None, description="Mã đơn (snake_case) nếu admin chỉ định.", max_length=60)
    note: str | None = Field(
        None,
        description="Yêu cầu thêm của admin về mẫu đơn (nguyên văn), vd 'ô lý do là bắt buộc'.",
        max_length=500,
    )


@REGISTRY.tool(
    "propose_template",
    "Leader/admin gửi một mẫu đơn (.docx, đơn trắng hoặc đã điền thông tin mẫu) và muốn THÊM vào hệ thống: tẩy trắng "
    "thông tin mẫu, nhận diện các trường, điền thử, rồi hiện thẻ xem trước kèm nút Kích hoạt/Huỷ.",
    LEADER_ADMIN,
    ProposeArgs,
    timeout_s=300,
)
async def propose_template(svc: Services, ctx: UserContext, a: ProposeArgs) -> ToolResult:
    return _result(
        await TemplateReview(svc).propose(
            a.upload_id, ctx, roles=list(a.roles or []), title=a.title, form_id=a.form_id, note=a.note
        )
    )


class UpdateTemplateArgs(BaseModel):
    template_draft_id: str = TD_ID
    title: str | None = Field(None, description="Tên đơn mới.", max_length=200)
    description: str | None = Field(None, description="Mô tả mới (1–2 câu).", max_length=500)
    roles: list[RoleIn] | None = Field(None, description="Danh sách MỚI đầy đủ những ai được dùng đơn.")
    form_id: str | None = Field(None, description="Mã đơn mới (snake_case).", max_length=60)
    intro: str | None = Field(None, description="Câu mời khi gửi mẫu cho người dùng.", max_length=300)
    changes: list[FieldChange] = Field(
        default_factory=list,
        description="Các thay đổi trường theo yêu cầu admin: update (đổi nhãn/kiểu/bắt buộc/lựa chọn/ai điền), "
        "remove, add (cần `anchor` là chữ nguyên văn trong đơn ngay trước ô trống).",
        max_length=40,
    )


@REGISTRY.tool(
    "update_template_draft",
    "Sửa bản duyệt mẫu đơn mới theo yêu cầu của admin (tên, ai được dùng, mã đơn, thêm/sửa/bỏ trường) rồi gửi "
    "thẻ xem trước mới; thẻ cũ hết hiệu lực.",
    LEADER_ADMIN,
    UpdateTemplateArgs,
    timeout_s=120,
)
async def update_template_draft(svc: Services, ctx: UserContext, a: UpdateTemplateArgs) -> ToolResult:
    return _result(
        await TemplateReview(svc).update(
            a.template_draft_id,
            ctx,
            changes=a.changes,
            title=a.title,
            description=a.description,
            roles=list(a.roles) if a.roles else None,
            form_id=a.form_id,
            intro=a.intro,
        )
    )


class TemplateDraftArgs(BaseModel):
    template_draft_id: str = TD_ID


@REGISTRY.tool(
    "cancel_template_draft",
    "Huỷ bản duyệt mẫu đơn mới khi admin không muốn thêm nữa.",
    LEADER_ADMIN,
    TemplateDraftArgs,
)
async def cancel_template_draft(svc: Services, ctx: UserContext, a: TemplateDraftArgs) -> ToolResult:
    return _result(await TemplateReview(svc).cancel(a.template_draft_id, ctx))
