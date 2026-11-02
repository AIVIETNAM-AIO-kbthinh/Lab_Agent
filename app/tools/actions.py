"""Tool tạo yêu cầu ghi chờ xác nhận: trả / gia hạn thiết bị, thay đổi thành viên.

Tool chỉ kiểm tra và tạo bản tóm tắt + nút Xác nhận; việc ghi chỉ xảy ra khi người dùng bấm nút.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from app.auth.roles import ADMIN_ONLY, BORROWERS, UserContext
from app.tools.registry import REGISTRY, ToolResult
from app.workflows import equipment as wf_equipment
from app.workflows import members as wf_members

if TYPE_CHECKING:
    from app.services import Services


class ReturnItem(BaseModel):
    ma_phieu: str | None = Field(
        None, description="Mã phiếu (P-YYYYMMDD-NN) nếu biết; phiếu cũ không có mã thì bỏ trống."
    )
    thiet_bi: str | None = Field(
        None, description="Tên hoặc mã thiết bị trả. Bỏ trống + có ma_phieu = trả cả phiếu.", max_length=200
    )
    so_luong: int | None = Field(None, ge=1, description="Số lượng trả; bỏ trống = trả hết dòng đang mượn.")
    tinh_trang: Literal["Tốt", "Hỏng nhẹ", "Hỏng", "Mất"] = Field(
        description="Tình trạng khi trả, HỎI người dùng nếu họ chưa nói."
    )
    ghi_chu: str | None = Field(None, description="Mô tả hư hỏng (nếu có).", max_length=300)


class ReturnArgs(BaseModel):
    items: list[ReturnItem] = Field(min_length=1, max_length=20)


@REGISTRY.tool(
    "return_equipment",
    "Tạo yêu cầu trả thiết bị (cả phiếu, từng thiết bị, hoặc một phần số lượng). Trước đó gọi my_borrows để "
    "liệt kê và hỏi tình trạng khi trả. Người dùng phải bấm Xác nhận thì mới ghi sổ.",
    BORROWERS,
    ReturnArgs,
)
async def return_equipment(svc: Services, ctx: UserContext, a: ReturnArgs) -> ToolResult:
    data, ui = await wf_equipment.propose_return(svc, ctx, [i.model_dump() for i in a.items])
    return ToolResult(data, ui_actions=[ui])


class ExtendArgs(BaseModel):
    ma_phieu: str = Field(description="Mã phiếu cần gia hạn.", max_length=40)
    han_tra_moi: str = Field(description="Hạn trả mới, NGUYÊN VĂN người dùng (vd '25/10', 'thứ 6 tuần sau').")
    thiet_bi: str | None = Field(
        None, description="Chỉ gia hạn một thiết bị trong phiếu; bỏ trống = cả phiếu."
    )


@REGISTRY.tool(
    "extend_borrow",
    "Tạo yêu cầu gia hạn hạn trả cho cả phiếu hoặc một thiết bị. Người dùng phải bấm Xác nhận thì mới ghi sổ.",
    BORROWERS,
    ExtendArgs,
)
async def extend_borrow(svc: Services, ctx: UserContext, a: ExtendArgs) -> ToolResult:
    data, ui = await wf_equipment.propose_extend(svc, ctx, a.ma_phieu, a.han_tra_moi, a.thiet_bi)
    return ToolResult(data, ui_actions=[ui])


class MemberChangeArgs(BaseModel):
    action: Literal["approve_application", "reject_application", "remove_member", "set_role"] = Field(
        description="approve/reject_application: duyệt/từ chối đơn gia nhập (cần submission_id); "
        "remove_member: cho thành viên rời lab (cần ho_ten); set_role: đổi vai trò (cần ho_ten, role)."
    )
    submission_id: str | None = Field(None, description="Mã đơn gia nhập (từ list_pending_applications).")
    ho_ten: str | None = Field(
        None,
        description="Họ tên thành viên; với approve: tên mới có phần phân biệt nếu trùng.",
        max_length=120,
    )
    role: Literal["leader", "thu_ki", "member"] | None = Field(None, description="Vai trò mới (set_role).")
    ly_do: str | None = Field(None, description="Lý do từ chối (không bắt buộc).", max_length=500)


@REGISTRY.tool(
    "request_member_change",
    "(Chỉ admin) Tạo yêu cầu duyệt/từ chối đơn gia nhập, cho thành viên rời lab, hoặc đổi leader/thư ký. Trả "
    "bản tóm tắt chờ người dùng bấm Xác nhận.",
    ADMIN_ONLY,
    MemberChangeArgs,
)
async def request_member_change(svc: Services, ctx: UserContext, a: MemberChangeArgs) -> ToolResult:
    if a.action in ("approve_application", "reject_application") and not a.submission_id:
        return ToolResult(
            {"error": "Cần submission_id (gọi list_pending_applications để lấy)."}, is_error=True
        )
    if a.action in ("remove_member", "set_role") and not a.ho_ten:
        return ToolResult({"error": "Cần ho_ten của thành viên."}, is_error=True)
    if a.action == "approve_application":
        data, ui = await wf_members.propose_approve(svc, ctx, a.submission_id or "", a.ho_ten)
    elif a.action == "reject_application":
        data, ui = await wf_members.propose_reject(svc, ctx, a.submission_id or "", a.ly_do)
    elif a.action == "remove_member":
        data, ui = await wf_members.propose_remove(svc, ctx, a.ho_ten or "")
    else:
        if not a.role:
            return ToolResult({"error": "Cần role mới: leader, thu_ki hoặc member."}, is_error=True)
        data, ui = await wf_members.propose_set_role(svc, ctx, a.ho_ten or "", a.role)
    return ToolResult(data, ui_actions=[ui])
