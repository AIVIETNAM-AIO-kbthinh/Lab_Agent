"""Đăng nhập email + mật khẩu do admin cấp; dev-login chỉ khi APP_ENV != production và test_mode."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.session_mw import issue_session
from app.auth import accounts, ratelimit, sessions
from app.auth.csrf import make_token
from app.auth.deps import (
    ACCOUNT_REVOKED,
    BYPASS_ROLE_COOKIE,
    SessionInfo,
    account_context,
    client_ip,
    current_account,
    current_session,
    current_user,
    get_services,
    require_csrf,
)
from app.auth.roles import Role
from app.config import auth_bypass_enabled, dev_login_enabled
from app.db.models import Session as SessionRow
from app.domain.audit import AuditLog
from app.errors import RateLimited, ValidationFailed
from app.logging import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class DevLoginIn(BaseModel):
    name: str = Field(max_length=200)


class DevRoleIn(BaseModel):
    role: Role


@router.get("/me")
async def me(request: Request) -> dict[str, object]:
    svc = get_services(request)
    info = current_session(request)
    ctx = await current_user(request)
    revoked = bool(getattr(request.state, "account_revoked", False))
    acc = None if revoked else await current_account(request)
    return {
        "role": ctx.role.value,
        "email": ctx.email,
        "ho_ten": ctx.ho_ten,
        "logged_in": not info.guest and not revoked,
        "notice": ACCOUNT_REVOKED if revoked else None,
        "must_change_password": bool(acc and acc.must_change_password),
        "dev_login": dev_login_enabled(svc.settings, svc.config),
        "auth_bypass": auth_bypass_enabled(svc.settings, svc.config),
        "test_mode": svc.config.test_mode,
        "lab_name": svc.config.lab.name,
        "csrf": make_token(svc.settings.app_secret_key.get_secret_value(), info.public_id),
    }


async def _rotate(
    request: Request,
    response: Response,
    info: SessionInfo,
    *,
    account_id: str | None = None,
    dev_login_name: str | None = None,
) -> None:
    """Đổi session sau khi đăng nhập/đăng xuất (chống session fixation)."""
    svc = get_services(request)
    async with svc.sm() as s, s.begin():
        old = await s.get(SessionRow, info.id_hash)
        if old is not None:
            await s.delete(old)
        token, row = await sessions.create_session(
            s,
            svc.config.auth,
            account_id=account_id,
            dev_login_name=dev_login_name,
            ip=client_ip(request, svc.config.security.trusted_proxies),
            user_agent=request.headers.get("user-agent"),
        )
        public_id = row.public_id
    issue_session(request, response, token, public_id)


@router.post("/login", dependencies=[Depends(require_csrf)])
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, object]:
    svc = get_services(request)
    info = current_session(request)
    ip = client_ip(request, svc.config.security.trusted_proxies)
    try:
        async with svc.sm() as s, s.begin():
            await ratelimit.hit(s, f"login:ip:{ip}", svc.config.security.login_attempts_per_ip_15min, 900)
    except RateLimited as e:
        raise HTTPException(status_code=429, detail=e.user_message) from None
    async with svc.sm() as s, s.begin():
        result = await accounts.authenticate(s, svc.config.auth, body.email, body.password)
        account_id = result.account.id if result else None
        account_email = result.account.email if result else ""
        must_change = bool(result and result.must_change_password)
    if account_id is None:
        log.info("login_failed")  # không log email
        raise HTTPException(status_code=401, detail=accounts.GENERIC_LOGIN_ERROR)
    # Đúng mật khẩu nhưng email không còn trong Members/admins ⇒ 403 (chỉ báo SAU khi đúng mật khẩu nên
    # không lộ email nào có tài khoản). Members lỗi tạm thời thì vẫn cho đăng nhập (role sẽ là guest tạm).
    not_member = False
    try:
        found = await account_context(
            svc, account_email, session_id=info.public_id, account_id=account_id, ip=ip
        )
        not_member = found is None
    except Exception as e:
        log.warning("login_members_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}")
    if not_member:
        log.info("login_not_member", account_id=account_id)
        raise HTTPException(status_code=403, detail=ACCOUNT_REVOKED)
    await _rotate(request, response, info, account_id=account_id)
    log.info("login_ok", account_id=account_id)
    return {"ok": True, "must_change_password": must_change}


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request, response: Response) -> dict[str, bool]:
    await _rotate(request, response, current_session(request))
    return {"ok": True}


@router.post("/change-password", dependencies=[Depends(require_csrf)])
async def change_password(body: ChangePasswordIn, request: Request) -> dict[str, bool]:
    svc = get_services(request)
    info = current_session(request)
    if not info.account_id:
        raise HTTPException(status_code=401, detail="Bạn cần đăng nhập.")
    try:
        async with svc.sm() as s, s.begin():
            await accounts.change_password(
                s,
                svc.config.auth,
                info.account_id,
                body.old_password,
                body.new_password,
                keep_session_hash=info.id_hash,
            )
    except ValidationFailed as e:
        raise HTTPException(status_code=400, detail=e.user_message) from None
    return {"ok": True}


@router.get("/dev-login")
async def dev_login_names(request: Request) -> dict[str, object]:
    svc = get_services(request)
    if not dev_login_enabled(svc.settings, svc.config):
        raise HTTPException(status_code=404)
    snap = await svc.members.snapshot()
    return {"members": [{"ho_ten": m.ho_ten, "role": m.role} for m in snap.active]}


@router.post("/dev-login", dependencies=[Depends(require_csrf)])
async def dev_login(body: DevLoginIn, request: Request, response: Response) -> dict[str, object]:
    svc = get_services(request)
    if not dev_login_enabled(svc.settings, svc.config):
        raise HTTPException(status_code=404)
    snap = await svc.members.snapshot()
    m = snap.by_name(body.name)
    if m is None:
        raise HTTPException(status_code=400, detail="Không có thành viên này trong Members.")
    await _rotate(request, response, current_session(request), dev_login_name=m.ho_ten)
    audit: AuditLog = svc.audit
    await audit.log(f"dev-login ({m.ho_ten})", m.role, "dev_login", m.ho_ten)
    return {"ok": True, "ho_ten": m.ho_ten, "role": m.role}


@router.post("/dev-role", dependencies=[Depends(require_csrf)])
async def dev_role(body: DevRoleIn, request: Request, response: Response) -> dict[str, object]:
    """Chế độ auth_bypass: đổi vai trò đang thử (guest/member/thu_ki/leader/admin) mà không cần đăng nhập."""
    svc = get_services(request)
    if not auth_bypass_enabled(svc.settings, svc.config):
        raise HTTPException(status_code=404)
    response.set_cookie(BYPASS_ROLE_COOKIE, body.role.value, httponly=True, samesite="lax", path="/")
    return {"ok": True, "role": body.role.value}
