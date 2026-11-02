"""Xác định vai trò (authentication_plan.md §1). Role luôn do backend gắn, lời tự xưng trong chat không có tác dụng."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.domain.members import Member, MembersSnapshot


class Role(StrEnum):
    GUEST = "guest"
    MEMBER = "member"
    THU_KI = "thu_ki"
    LEADER = "leader"
    ADMIN = "admin"


# Nhóm quyền (authentication_plan.md §1): các nhóm cộng dồn, admin có tất cả.
ALL_ROLES = frozenset(Role)
MEMBER_PLUS = frozenset({Role.MEMBER, Role.THU_KI, Role.LEADER, Role.ADMIN})
STAFF = frozenset({Role.THU_KI, Role.LEADER, Role.ADMIN})  # thấy SĐT, MSSV, cựu thành viên; đẩy tài liệu KB
BORROWERS = frozenset({Role.MEMBER, Role.THU_KI, Role.LEADER, Role.ADMIN})  # mượn / trả / gia hạn
LEADER_ADMIN = frozenset({Role.LEADER, Role.ADMIN})  # xem đơn chờ duyệt, mẫu đơn, sửa data/
ADMIN_ONLY = frozenset({Role.ADMIN})  # nhân sự, tài khoản, AuditLog, chi phí, KB web


@dataclass(frozen=True)
class UserContext:
    """Ngữ cảnh người dùng tiêm vào tool. KHÔNG có trong schema gửi cho LLM."""

    role: Role
    session_id: str  # public id của session
    email: str | None = None
    account_id: str | None = None
    member: Member | None = None
    dev_login: bool = False
    ip: str | None = None
    name: str | None = None  # họ tên của admin lấy từ config (admin không cần có trong Members)

    @property
    def ho_ten(self) -> str | None:
        if self.name:
            return self.name
        return self.member.ho_ten if self.member else None

    @property
    def is_member_plus(self) -> bool:
        return self.role in MEMBER_PLUS

    @property
    def actor(self) -> str:
        """Giá trị cột actor trong AuditLog."""
        if self.role == Role.GUEST and not self.email:
            return f"guest:{self.session_id[:8]}"
        base = self.email or f"session:{self.session_id[:8]}"
        if self.dev_login and self.ho_ten:
            return f"{base} ({self.ho_ten})"
        if self.role == Role.ADMIN and self.name:
            return f"{base} ({self.name})"
        return base


def _member_role(m: Member) -> Role:
    return Role(m.role) if m.role in {"leader", "thu_ki", "member"} else Role.MEMBER


def resolve_role(
    email: str | None, snapshot: MembersSnapshot | None, admins: Iterable[str]
) -> tuple[Role, Member | None]:
    """Nguồn duy nhất xác định role theo email: admin (config) > Members > guest.

    `snapshot` = None khi không đọc được Members: admin vẫn là admin, người khác là guest.
    """
    if not email:
        return Role.GUEST, None
    e = email.strip().lower()
    member = snapshot.by_email(e) if snapshot is not None else None
    if e in {a.strip().lower() for a in admins}:
        return Role.ADMIN, member
    if member is None:
        return Role.GUEST, None
    return _member_role(member), member


def resolve_dev_login(name: str, snapshot: MembersSnapshot) -> tuple[Role, Member | None]:
    """Chế độ test: đăng nhập với tư cách <họ tên> (mọi người chung một email)."""
    m = snapshot.by_name(name)
    if m is None:
        return Role.GUEST, None
    return _member_role(m), m
