"""FastAPI dependency: Services, session hiện tại, UserContext (role resolve từ Members mỗi request, cache ngắn)."""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from app.auth import csrf
from app.auth.roles import Role, UserContext, resolve_dev_login, resolve_role
from app.config import auth_bypass_enabled, dev_login_enabled
from app.db.models import Account
from app.db.models import Session as SessionRow
from app.logging import get_logger

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


@dataclass
class SessionInfo:
    id_hash: str
    public_id: str
    account_id: str | None
    dev_login_name: str | None
    guest: bool


def get_services(request: Request) -> Services:
    return request.app.state.services  # type: ignore[no-any-return]


def client_ip(request: Request, trusted_proxies: list[str]) -> str:
    peer = request.client.host if request.client else "0.0.0.0"  # noqa: S104
    fwd = request.headers.get("x-forwarded-for")
    if fwd and _trusted(peer, trusted_proxies):
        return fwd.split(",")[0].strip()[:64]
    return peer


def _trusted(ip: str, proxies: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(p, strict=False) for p in proxies)
    except ValueError:
        return False


def current_session(request: Request) -> SessionInfo:
    info: SessionInfo | None = getattr(request.state, "session", None)
    if info is None:
        raise HTTPException(status_code=401, detail="Phiên làm việc không hợp lệ, vui lòng tải lại trang.")
    return info


def require_csrf(request: Request) -> None:
    svc = get_services(request)
    info = current_session(request)
    ok = csrf.check(
        svc.settings.app_secret_key.get_secret_value(),
        info.public_id,
        request.cookies.get(csrf.COOKIE),
        request.headers.get(csrf.HEADER),
    )
    if not ok:
        raise HTTPException(status_code=403, detail="CSRF token không hợp lệ, vui lòng tải lại trang.")


async def current_account(request: Request) -> Account | None:
    info = current_session(request)
    if not info.account_id:
        return None
    svc = get_services(request)
    async with svc.sm() as s:
        acc = await s.get(Account, info.account_id)
    if acc is None or acc.disabled:
        return None
    return acc


BYPASS_ROLE_COOKIE = "la_dev_role"
ACCOUNT_REVOKED = "Tài khoản không còn hiệu lực. Liên hệ admin nếu bạn nghĩ đây là nhầm lẫn."


def bypass_role(request: Request) -> Role:
    svc = get_services(request)
    raw = request.cookies.get(BYPASS_ROLE_COOKIE) or svc.config.test.bypass_role
    try:
        return Role(raw)
    except ValueError:
        return Role(svc.config.test.bypass_role)


async def _bypass_user(request: Request, info: SessionInfo, ip: str) -> UserContext:
    """Chế độ auth_bypass (chỉ dev): vai trò lấy từ cookie do giao diện chọn, gắn thành viên đầu tiên
    có vai trò đó trong Members (để các tool cá nhân như my_borrows có người dùng). Không đọc được Members
    thì vẫn giữ vai trò, chỉ không có họ tên. Admin: admin đầu tiên trong config (có họ tên riêng)."""
    svc = get_services(request)
    role = bypass_role(request)
    member = None
    email: str | None = None
    name: str | None = None
    if role == Role.ADMIN:
        adm = svc.config.auth.admins[0] if svc.config.auth.admins else None
        email = adm.email if adm else None
        name = (adm.ho_ten or None) if adm else None
    elif role != Role.GUEST:
        try:
            snap = await svc.members.snapshot()
            member = next((m for m in snap.active if m.role == role.value), None)
            if member is None and role == Role.MEMBER and snap.active:
                member = snap.active[0]
        except Exception as e:
            log.warning("bypass_members_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}")
        email = member.email if member else None
    return UserContext(
        role=role,
        session_id=info.public_id,
        email=email,
        member=member,
        dev_login=True,
        ip=ip,
        name=name,
    )


async def account_context(
    svc: Services, email: str, *, session_id: str, account_id: str | None, ip: str | None
) -> UserContext | None:
    """UserContext của một tài khoản theo email; None = email không còn trong Members và không phải admin.

    Không đọc được Members: admin vẫn là admin; người khác ném lỗi để caller hạ về guest TẠM THỜI.
    """
    admins = svc.config.auth.admin_emails()
    adm = svc.config.auth.admin(email)
    try:
        snap = await svc.members.snapshot()
    except Exception:
        if adm is None:
            raise
        snap = None
    role, member = resolve_role(email, snap, admins)
    if role == Role.GUEST:
        return None
    return UserContext(
        role=role,
        session_id=session_id,
        email=email.strip().lower(),
        account_id=account_id,
        member=member,
        ip=ip,
        name=(adm.ho_ten or None) if adm else None,
    )


async def _revoke_session(svc: Services, info: SessionInfo) -> None:
    async with svc.sm() as s, s.begin():
        row = await s.get(SessionRow, info.id_hash)
        if row is not None:
            await s.delete(row)


async def current_user(request: Request) -> UserContext:
    cached: UserContext | None = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    svc = get_services(request)
    info = current_session(request)
    ip = client_ip(request, svc.config.security.trusted_proxies)
    if auth_bypass_enabled(svc.settings, svc.config):
        request.state.user = await _bypass_user(request, info, ip)
        return request.state.user  # type: ignore[no-any-return]
    ctx = UserContext(role=Role.GUEST, session_id=info.public_id, ip=ip)
    try:
        if info.dev_login_name and dev_login_enabled(svc.settings, svc.config):
            snap = await svc.members.snapshot()
            role, member = resolve_dev_login(info.dev_login_name, snap)
            ctx = UserContext(
                role=role,
                session_id=info.public_id,
                email=member.email if member else None,
                member=member,
                dev_login=True,
                ip=ip,
            )
        elif info.account_id:
            acc = await current_account(request)
            if acc is not None:
                found = await account_context(
                    svc, acc.email, session_id=info.public_id, account_id=acc.id, ip=ip
                )
                if found is None:
                    # email không còn trong Members/admins ⇒ huỷ session, báo "Tài khoản không còn hiệu lực"
                    await _revoke_session(svc, info)
                    request.state.account_revoked = True
                    log.info("session_revoked_not_member", account_id=acc.id)
                else:
                    ctx = found
    except Exception as e:
        # không đọc được Members ⇒ guest TẠM THỜI, KHÔNG huỷ session; ghi log để admin biết
        log.error("role_resolve_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        ctx = UserContext(role=Role.GUEST, session_id=info.public_id, account_id=info.account_id, ip=ip)
    request.state.user = ctx
    return ctx


def require_role(*roles: Role | Iterable[Role]) -> Callable[[Request], Awaitable[UserContext]]:
    """Dependency khai báo quyền: `Depends(require_role(Role.LEADER, Role.ADMIN))` hoặc `require_role(STAFF)`."""
    allowed: set[Role] = set()
    for r in roles:
        if isinstance(r, Role):
            allowed.add(r)
        else:
            allowed.update(r)
    frozen = frozenset(allowed)

    async def dep(request: Request) -> UserContext:
        ctx = await current_user(request)
        if ctx.role not in frozen:
            raise HTTPException(status_code=403, detail="Bạn không có quyền truy cập chức năng này.")
        return ctx

    return dep


async def require_admin(request: Request) -> UserContext:
    ctx = await current_user(request)
    if ctx.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Chỉ admin được truy cập.")
    return ctx


require_leader_admin = require_role(Role.LEADER, Role.ADMIN)
