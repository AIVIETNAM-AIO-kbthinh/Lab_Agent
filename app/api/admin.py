"""Trang admin: KB web (danh sách trang, crawl ngay, áp dụng run bị chặn), op FAILED, chi phí LLM, tài khoản,
AuditLog. Chỉ admin (mục mẫu đơn cho leader nằm ở app/api/templates.py)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app import clock
from app.auth import accounts
from app.auth.account_mail import is_lab_email, send_temp_password
from app.auth.deps import get_services, require_admin, require_csrf
from app.auth.roles import UserContext
from app.db.models import LLMUsage, OutboxOp, WebCrawlRun, WebPage
from app.errors import NotFound, ValidationFailed
from app.llm.usage import month_window
from app.outbox.steps import enqueue

router = APIRouter(prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/kb/web")
async def web_pages(request: Request, status: str | None = None) -> dict[str, Any]:
    svc = get_services(request)
    async with svc.sm() as s:
        q = select(WebPage).order_by(WebPage.url_canonical, WebPage.lang)
        if status:
            q = q.where(WebPage.status == status)
        pages = (await s.scalars(q.limit(2000))).all()
        runs = (await s.scalars(select(WebCrawlRun).order_by(WebCrawlRun.started_at.desc()).limit(20))).all()
    return {
        "pages": [
            {
                "url": p.url_canonical,
                "lang": p.lang,
                "title": p.title,
                "status": p.status,
                "doc_date": p.doc_date,
                "last_fetched": p.last_fetched,
                "last_changed": p.last_changed,
                "missing_count": p.missing_count,
                "chunks": p.chunk_count,
            }
            for p in pages
        ],
        "runs": [
            {
                "id": r.id,
                "source": r.source_id,
                "mode": r.mode,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "stats": r.stats_json,
                "error": r.error,
            }
            for r in runs
        ],
    }


class CrawlIn(BaseModel):
    mode: Literal["quick", "full"] = "quick"


@router.post("/kb/web/crawl", dependencies=[Depends(require_csrf)])
async def crawl_now(
    body: CrawlIn, request: Request, admin: UserContext = Depends(require_admin)
) -> dict[str, Any]:
    import app.jobs.web_crawl  # noqa: F401  (đăng ký op)

    svc = get_services(request)
    async with svc.sm() as s, s.begin():
        op_id = await enqueue(s, "web_crawl", {"mode": body.mode}, actor=admin.actor)
    return {"op_id": op_id, "status": "queued"}


@router.post("/kb/web/runs/{run_id}/apply", dependencies=[Depends(require_csrf)])
async def apply_run(
    run_id: int, request: Request, admin: UserContext = Depends(require_admin)
) -> dict[str, Any]:
    import app.jobs.web_crawl  # noqa: F401

    svc = get_services(request)
    async with svc.sm() as s, s.begin():
        run = await s.get(WebCrawlRun, run_id)
        if run is None or run.status != "BLOCKED":
            raise HTTPException(status_code=400, detail="Run không ở trạng thái BLOCKED.")
        op_id = await enqueue(
            s,
            "web_crawl_apply",
            {"run_id": run_id, "source_id": run.source_id},
            actor=admin.actor,
            op_id=f"apply_run_{run_id}",
        )
    return {"op_id": op_id, "status": "queued"}


@router.get("/ops")
async def ops(request: Request, status: str = "FAILED") -> dict[str, Any]:
    svc = get_services(request)
    async with svc.sm() as s:
        rows = (
            await s.scalars(
                select(OutboxOp)
                .where(OutboxOp.status == status)
                .order_by(OutboxOp.updated_at.desc())
                .limit(200)
            )
        ).all()
    return {
        "ops": [
            {
                "id": o.id,
                "kind": o.kind,
                "status": o.status,
                "attempts": o.attempts,
                "actor": o.actor,
                "error": o.last_error,
                "updated_at": o.updated_at,
            }
            for o in rows
        ]
    }


@router.get("/llm/costs")
async def costs(request: Request) -> dict[str, Any]:
    svc = get_services(request)
    start, _ = month_window()
    day = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
    async with svc.sm() as s:
        rows = (
            await s.execute(
                select(
                    LLMUsage.profile,
                    LLMUsage.model,
                    func.count(),
                    func.sum(LLMUsage.input_tokens),
                    func.sum(LLMUsage.output_tokens),
                    func.sum(LLMUsage.cache_read_tokens),
                    func.sum(LLMUsage.cost_usd),
                    func.avg(LLMUsage.first_token_ms),
                )
                .where(LLMUsage.ts >= start)
                .group_by(LLMUsage.profile, LLMUsage.model)
            )
        ).all()
        today = float(
            await s.scalar(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(LLMUsage.ts >= day))
            or 0.0
        )
        p95 = await s.scalar(
            select(func.percentile_cont(0.95).within_group(LLMUsage.first_token_ms)).where(
                LLMUsage.ts >= day - timedelta(days=7), LLMUsage.first_token_ms.is_not(None)
            )
        )
    b = svc.config.llm.budgets
    return {
        "month": [
            {
                "profile": r[0],
                "model": r[1],
                "calls": r[2],
                "input": r[3],
                "output": r[4],
                "cache_read": r[5],
                "cost_usd": round(float(r[6] or 0), 4),
                "avg_first_token_ms": int(r[7] or 0),
            }
            for r in rows
        ],
        "month_total_usd": round(sum(float(r[6] or 0) for r in rows), 4),
        "today_usd": round(today, 4),
        "p95_first_token_ms_7d": int(p95) if p95 else None,
        "alert_usd": b.lab_usd_per_month_alert,
        "hard_stop_usd": b.lab_usd_per_month_hard_stop,
    }


# ------------------------------------------------------------------ tài khoản


class AccountIn(BaseModel):
    email: str = Field(max_length=320)


class AccountAction(BaseModel):
    email: str = Field(max_length=320)
    action: Literal["reset", "disable", "enable", "unlock", "delete"]


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    svc = get_services(request)
    async with svc.sm() as s:
        accs = await accounts.list_accounts(s)
    now = clock.now()
    try:
        snap = await svc.members.snapshot()
    except Exception:
        snap = None
    out = []
    for a in accs:
        adm = svc.config.auth.admin(a.email)
        m = snap.by_email(a.email) if snap is not None else None
        if adm is not None:
            who, valid = f"Admin · {adm.ho_ten}" if adm.ho_ten else "Admin", True
        elif m is not None:
            who, valid = f"{m.ho_ten} · {m.role_raw or m.role}", True
        else:
            who, valid = ("không còn trong Members" if snap is not None else "?"), snap is None
        out.append(
            {
                "email": a.email,
                "disabled": a.disabled,
                "must_change_password": a.must_change_password,
                "locked": bool(a.locked_until and a.locked_until > now),
                "last_login_at": a.last_login_at,
                "who": who,
                "valid": valid,
            }
        )
    return {"accounts": out}


@router.post("/accounts", dependencies=[Depends(require_csrf)])
async def create_account(
    body: AccountIn, request: Request, admin: UserContext = Depends(require_admin)
) -> dict[str, Any]:
    svc = get_services(request)
    email = accounts.norm_email(body.email)
    try:
        ok = await is_lab_email(svc, email)
    except Exception:
        raise HTTPException(status_code=503, detail="Không đọc được Members, thử lại sau.") from None
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Chỉ tạo tài khoản cho email có trong Members hoặc trong danh sách admin (config).",
        )
    try:
        async with svc.sm() as s, s.begin():
            acc, pw = await accounts.create_account(s, email)
    except ValidationFailed as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    mailed = await send_temp_password(svc, acc.email, pw, str(request.base_url) + "login")
    await svc.audit.log(admin.actor, "admin", "account_create", acc.email, True, {"mailed": mailed})
    return {"email": acc.email, "mailed": mailed, **({} if mailed else {"temp_password": pw})}


@router.post("/accounts/action", dependencies=[Depends(require_csrf)])
async def account_action(
    body: AccountAction, request: Request, admin: UserContext = Depends(require_admin)
) -> dict[str, Any]:
    svc = get_services(request)
    out: dict[str, Any] = {"email": body.email, "action": body.action}
    if body.action == "delete" and accounts.norm_email(body.email) == (admin.email or ""):
        raise HTTPException(status_code=400, detail="Không tự xoá tài khoản đang đăng nhập.")
    try:
        async with svc.sm() as s, s.begin():
            if body.action == "reset":
                pw = await accounts.reset_password(s, body.email)
            elif body.action in ("disable", "enable"):
                await accounts.set_disabled(s, body.email, body.action == "disable")
            elif body.action == "delete":
                await accounts.delete_account(s, body.email)
            else:
                await accounts.unlock(s, body.email)
    except NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    if body.action == "reset":
        out["mailed"] = await send_temp_password(svc, body.email, pw, str(request.base_url) + "login")
        if not out["mailed"]:
            out["temp_password"] = pw
    await svc.audit.log(admin.actor, "admin", f"account_{body.action}", body.email, True)
    return out


# ------------------------------------------------------------------ AuditLog (chỉ xem)


@router.get("/audit")
async def audit_log(
    request: Request, limit: int = 200, q: str | None = None, action: str | None = None
) -> dict[str, Any]:
    """Các dòng AuditLog mới nhất (đọc trực tiếp sheet). Không có endpoint sửa/xoá: AuditLog không ai sửa."""
    svc = get_services(request)
    try:
        rows = await svc.audit.recent(limit=max(1, min(limit, 1000)), query=q, action=action)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Không đọc được AuditLog: {type(e).__name__}") from None
    return {"rows": rows, "count": len(rows)}
