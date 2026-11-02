"""Middleware: request_id, session (tạo session khách khi chưa có), cookie CSRF, security headers."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from app import logging as applog
from app import metrics
from app.auth import csrf, sessions
from app.auth.deps import SessionInfo, client_ip, get_services

NO_SESSION_PREFIXES = ("/static/", "/healthz", "/readyz", "/metrics", "/favicon")


def _secure(request: Request) -> bool:
    svc = get_services(request)
    v = svc.config.security.cookie_secure
    return svc.settings.is_production if v is None else v


def issue_session(request: Request, response: Response, token: str, public_id: str) -> None:
    svc = get_services(request)
    secure = _secure(request)
    max_age = svc.config.auth.session_max_days * 86400
    response.set_cookie(
        sessions.COOKIE, token, max_age=max_age, httponly=True, secure=secure, samesite="lax", path="/"
    )
    response.set_cookie(
        csrf.COOKIE,
        csrf.make_token(svc.settings.app_secret_key.get_secret_value(), public_id),
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )
    request.state.session_issued = True


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)


async def app_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    rid = request.headers.get("x-request-id", "")[:32] or applog.new_request_id()
    applog.clear()
    applog.bind(request_id=rid)
    request.state.request_id = rid
    path = request.url.path
    t0 = time.monotonic()
    new_token: tuple[str, str] | None = None

    if not path.startswith(NO_SESSION_PREFIXES):
        svc = get_services(request)
        async with svc.sm() as s, s.begin():
            row = await sessions.load_session(s, svc.config.auth, request.cookies.get(sessions.COOKIE))
            if row is None:
                token, row = await sessions.create_session(
                    s,
                    svc.config.auth,
                    ip=client_ip(request, svc.config.security.trusted_proxies),
                    user_agent=request.headers.get("user-agent"),
                )
                new_token = (token, row.public_id)
            request.state.session = SessionInfo(
                row.id_hash, row.public_id, row.account_id, row.dev_login_name, row.guest
            )

    response = await call_next(request)

    if new_token and not getattr(request.state, "session_issued", False):
        issue_session(request, response, *new_token)
    elif getattr(request.state, "session", None) is not None and not getattr(
        request.state, "session_issued", False
    ):
        svc = get_services(request)
        want = csrf.make_token(
            svc.settings.app_secret_key.get_secret_value(), request.state.session.public_id
        )
        if request.cookies.get(csrf.COOKIE) != want:
            response.set_cookie(
                csrf.COOKIE,
                want,
                httponly=False,
                secure=_secure(request),
                samesite="lax",
                path="/",
                max_age=svc.config.auth.session_max_days * 86400,
            )

    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if _secure_req(request):
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    h["X-Request-ID"] = rid
    route = request.scope.get("route")
    label = getattr(route, "path", "unmatched")
    metrics.HTTP_REQUESTS.labels(request.method, label, str(response.status_code)).inc()
    metrics.HTTP_LATENCY.labels(label).observe(time.monotonic() - t0)
    return response


def _secure_req(request: Request) -> bool:
    try:
        return _secure(request) and (
            request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        )
    except Exception:
        return False
