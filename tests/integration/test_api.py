"""API: session khách, CSRF, đăng nhập + khoá tạm, đổi mật khẩu bắt buộc, dev-login, SSE chat, admin, upload."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from app.auth import accounts
from app.main import create_app
from tests.conftest import Env
from tests.fakes.llm import reply

pytestmark = pytest.mark.db


@pytest.fixture
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(services=env.svc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def boot(c: httpx.AsyncClient) -> dict[str, object]:
    r = await c.get("/auth/me")
    assert r.status_code == 200
    return dict(r.json())


def h(c: httpx.AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies.get("la_csrf") or ""}


async def make_account(env: Env, email: str, pw: str = "Mat-Khau-Rat-Dai-2026") -> None:
    async with env.svc.sm() as s, s.begin():
        await accounts.create_account(s, email, pw)


async def test_guest_session_and_security_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/auth/me")
    assert r.json()["role"] == "guest" and r.json()["logged_in"] is False
    assert "la_session" in client.cookies and "la_csrf" in client.cookies
    sc = r.headers.get_list("set-cookie")
    assert any("la_session=" in x and "HttpOnly" in x and "SameSite=lax" in x for x in sc)
    assert (
        r.headers["X-Frame-Options"] == "DENY"
        and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    )
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_csrf_required(client: httpx.AsyncClient, env: Env) -> None:
    await boot(client)
    r = await client.post("/auth/login", json={"email": "an@gmail.com", "password": "x"})
    assert r.status_code == 403
    r = await client.post(
        "/auth/login", json={"email": "an@gmail.com", "password": "x"}, headers={"X-CSRF-Token": "forged"}
    )
    assert r.status_code == 403


async def test_login_lockout_generic_error(client: httpx.AsyncClient, env: Env) -> None:
    await make_account(env, "an@gmail.com")
    await boot(client)
    for _ in range(5):
        r = await client.post(
            "/auth/login", json={"email": "an@gmail.com", "password": "sai-mat-khau"}, headers=h(client)
        )
        assert r.status_code == 401
    # đúng mật khẩu nhưng đang bị khoá tạm ⇒ vẫn lỗi chung chung
    r = await client.post(
        "/auth/login", json={"email": "an@gmail.com", "password": "Mat-Khau-Rat-Dai-2026"}, headers=h(client)
    )
    assert r.status_code == 401
    unknown = await client.post(
        "/auth/login", json={"email": "nobody@gmail.com", "password": "x"}, headers=h(client)
    )
    assert unknown.json()["detail"] == r.json()["detail"]  # không lộ email có tồn tại hay không


async def test_login_must_change_password_then_chat(client: httpx.AsyncClient, env: Env) -> None:
    await make_account(env, "an@gmail.com")
    await boot(client)
    old_session = client.cookies.get("la_session")
    r = await client.post(
        "/auth/login", json={"email": "AN@gmail.com", "password": "Mat-Khau-Rat-Dai-2026"}, headers=h(client)
    )
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert client.cookies.get("la_session") != old_session  # đổi session sau đăng nhập
    me = (await client.get("/auth/me")).json()
    assert me["role"] == "member" and me["ho_ten"] == "Lê Văn An"
    r = await client.post("/chat", json={"message": "hi"}, headers=h(client))
    assert r.status_code == 403  # phải đổi mật khẩu trước
    r = await client.post(
        "/auth/change-password",
        json={"old_password": "Mat-Khau-Rat-Dai-2026", "new_password": "password123"},
        headers=h(client),
    )
    assert r.status_code == 400
    r = await client.post(
        "/auth/change-password",
        json={"old_password": "Mat-Khau-Rat-Dai-2026", "new_password": "Mot-Mat-Khau-Moi-2026!"},
        headers=h(client),
    )
    assert r.status_code == 200

    env.anthropic.steps = [reply(tool_calls=[("my_borrows", {})]), reply("Bạn đang mượn 1 máy chiếu.")]
    r = await client.post("/chat", json={"message": "mình đang mượn gì?"}, headers=h(client))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "conversation" and kinds[-1] == "done" and "tool" in kinds
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Bạn đang mượn 1 máy chiếu."
    hist = (await client.get(f"/chat/{events[0]['id']}/history")).json()["messages"]
    assert hist[0] == {"role": "user", "text": "mình đang mượn gì?"}


async def test_dev_login_and_admin_guard(client: httpx.AsyncClient, env: Env) -> None:
    await boot(client)
    names = (await client.get("/auth/dev-login")).json()["members"]
    assert len(names) == 4
    r = await client.post("/auth/dev-login", json={"name": "Nguyễn Văn Leader"}, headers=h(client))
    assert r.status_code == 200
    assert (await client.get("/auth/me")).json()["role"] == "leader"
    assert (await client.get("/admin/api/kb/web")).status_code == 403
    env.svc.settings.app_env = "production"  # production ⇒ dev-login biến mất
    assert (await client.get("/auth/dev-login")).status_code == 404
    env.svc.settings.app_env = "test"


async def test_admin_endpoints(client: httpx.AsyncClient, env: Env) -> None:
    await make_account(env, "admin@gmail.com")
    async with env.svc.sm() as s, s.begin():
        acc = await accounts.get_by_email(s, "admin@gmail.com")
        assert acc is not None
        acc.must_change_password = False
    await boot(client)
    await client.post(
        "/auth/login",
        json={"email": "admin@gmail.com", "password": "Mat-Khau-Rat-Dai-2026"},
        headers=h(client),
    )
    assert (await client.get("/auth/me")).json()["role"] == "admin"
    assert (await client.get("/admin/api/kb/web")).status_code == 200
    assert "month_total_usd" in (await client.get("/admin/api/llm/costs")).json()
    r = await client.post("/admin/api/kb/web/crawl", json={"mode": "quick"}, headers=h(client))
    assert r.json()["status"] == "queued"
    r = await client.post("/admin/api/accounts", json={"email": "binh@vnu.edu.vn"}, headers=h(client))
    assert r.json()["mailed"] is True and env.gmail.sent  # mật khẩu tạm qua mail, không trả về API


async def test_guest_rate_limit_and_upload(client: httpx.AsyncClient, env: Env) -> None:
    env.svc.config.security.guest_msgs_per_session_hour = 1
    await boot(client)
    env.openai.steps = [reply("xin chào")]
    assert (await client.post("/chat", json={"message": "a"}, headers=h(client))).status_code == 200
    assert (await client.post("/chat", json={"message": "b"}, headers=h(client))).status_code == 429
    ok = await client.post(
        "/uploads", files={"file": ("cv.pdf", b"%PDF-1.7 test", "application/pdf")}, headers=h(client)
    )
    assert ok.status_code == 200 and ok.json()["type"] == "pdf"
    bad = await client.post(
        "/uploads", files={"file": ("cv.pdf", b"MZ\x90\x00", "application/pdf")}, headers=h(client)
    )
    assert bad.status_code == 400


async def test_auth_bypass_role_switch(client: httpx.AsyncClient, env: Env) -> None:
    cfg = env.svc.config
    cfg.test_mode, cfg.test.auth_bypass, cfg.test.bypass_role = True, True, "member"
    try:
        me = await boot(client)
        assert me["auth_bypass"] is True and me["role"] == "member" and me["ho_ten"]
        r = await client.post("/auth/dev-role", json={"role": "admin"}, headers=h(client))
        assert r.status_code == 200
        assert (await client.get("/auth/me")).json()["role"] == "admin"
        assert (await client.get("/admin/api/kb/web")).status_code == 200
        await client.post("/auth/dev-role", json={"role": "guest"}, headers=h(client))
        assert (await client.get("/admin/api/kb/web")).status_code == 403
        assert (
            await client.post("/auth/dev-role", json={"role": "root"}, headers=h(client))
        ).status_code == 422
        env.svc.settings.app_env = "production"  # production ⇒ bypass không có hiệu lực
        assert (await client.get("/auth/me")).json()["auth_bypass"] is False
        assert (
            await client.post("/auth/dev-role", json={"role": "admin"}, headers=h(client))
        ).status_code == 404
    finally:
        env.svc.settings.app_env = "test"
        cfg.test.auth_bypass = False
