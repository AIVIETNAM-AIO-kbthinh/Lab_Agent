"""Chọn profile + system prompt theo role. System prompt cố định theo role (không chèn ngày/ID) để cache."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from app.auth.roles import Role, UserContext

PROMPTS = Path(__file__).parent / "prompts"
WEEKDAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]
ROLE_LABEL = {
    Role.GUEST: "khách",
    Role.MEMBER: "thành viên",
    Role.THU_KI: "thư ký",
    Role.LEADER: "leader",
    Role.ADMIN: "admin (thầy phụ trách)",
}


def profile_for(role: Role) -> str:
    return "guest_agent" if role == Role.GUEST else "member_agent"


@lru_cache(maxsize=8)
def _template(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


# Phạm vi quyền theo vai trò (authentication_plan.md §1, §3), nối vào cuối system prompt của member+.
# Cố định theo role nên không phá prompt cache. Đây chỉ là hướng dẫn cho LLM; quyền thật do backend kiểm tra.
ROLE_SCOPE = {
    Role.MEMBER: (
        "Người dùng là THÀNH VIÊN.\n"
        "- Được: tra thiết bị, lịch trực, thành viên (không có SĐT/MSSV của người khác), tài liệu nội bộ và biên "
        "bản họp; mượn/trả/gia hạn thiết bị; đăng ký trực của mình; xuất file (lịch sử đã trả và đơn đã nộp: chỉ "
        "của mình; biên bản họp: tất cả).\n"
        "- KHÔNG được: xem SĐT/MSSV người khác, cựu thành viên, đơn gia nhập (kể cả danh sách chờ duyệt); lập "
        "biên bản họp; đưa tài liệu vào kho; sửa dữ liệu lab; thêm mẫu đơn; duyệt đơn, cho rời lab, đổi vai trò."
    ),
    Role.THU_KI: (
        "Người dùng là THƯ KÝ.\n"
        "- Được: mọi quyền của thành viên; xem SĐT, MSSV của mọi thành viên (gọi `query_members`); xem/xuất cựu "
        "thành viên, toàn bộ lịch sử mượn, mọi đơn đã nộp kể cả đơn gia nhập (`export_form_files`); lập biên bản "
        "họp; đưa tài liệu vào kho (`publish_kb_document`).\n"
        "- KHÔNG được: xem danh sách đơn gia nhập CHỜ DUYỆT; duyệt/từ chối đơn; sửa dữ liệu lab (thiết bị, sổ "
        "mượn, lịch trực người khác); thêm mẫu đơn; cho rời lab, đổi vai trò. Những việc này cần leader hoặc admin."
    ),
    Role.LEADER: (
        "Người dùng là LEADER.\n"
        "- Được: mọi quyền của thư ký; xem danh sách đơn gia nhập chờ duyệt (chỉ xem); sửa dữ liệu lab trừ "
        "thông tin thành viên (`request_data_edit`); thêm/sửa/huỷ bản duyệt mẫu đơn.\n"
        "- KHÔNG được: duyệt/từ chối đơn gia nhập; cho thành viên rời lab; đổi vai trò; sửa thông tin thành viên; "
        "tài khoản, AuditLog, chi phí. Những việc này chỉ admin làm."
    ),
    Role.ADMIN: (
        "Người dùng là ADMIN (thầy phụ trách hoặc dev), có mọi quyền: thêm mọi quyền của leader là duyệt/từ chối "
        "đơn gia nhập, cho rời lab, đổi vai trò (`request_member_change`), sửa thông tin thành viên "
        "(`request_data_edit`, target = member). Tài khoản, AuditLog, chi phí, KB web: ở trang quản trị."
    ),
}

SCOPE_FOOTER = (
    "- Chỉ gọi tool có trong danh sách tool bạn được cung cấp. Việc nào không có tool tương ứng là ngoài quyền "
    "của người dùng: KHÔNG thử gọi, nói ngắn gọn là vai trò của họ không làm được và ai làm được.\n"
    '- Việc nằm trong phần "Được" thì làm ngay bằng tool, không tự từ chối.'
)


def system_prompt(role: Role, lab_name: str) -> str:
    if role == Role.GUEST:
        return _template("guest").replace("{lab_name}", lab_name)
    base = _template("member").replace("{lab_name}", lab_name)
    return f"{base}\n\n# Quyền của người dùng này\n{ROLE_SCOPE[role]}\n{SCOPE_FOOTER}\n"


def user_header(ctx: UserContext, today: date) -> str:
    """Thông tin thay đổi theo lượt đi trong tin nhắn user (không phải system) để không phá prompt cache."""
    day = f"{WEEKDAYS[today.weekday()]}, {today:%d/%m/%Y}"
    if ctx.role == Role.GUEST:
        return f"[Hôm nay: {day}]"
    who = f"{ctx.ho_ten}, " if ctx.ho_ten else ""
    return f"[Hôm nay: {day} | Người dùng: {who}vai trò {ROLE_LABEL[ctx.role]}]"
