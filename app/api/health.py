"""/healthz (sống), /readyz (DB, Qdrant, token Google), /metrics (chỉ mạng nội bộ)."""

from __future__ import annotations

import ipaddress
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.auth.deps import get_services

router = APIRouter(tags=["health"])
_google_cache: dict[str, Any] = {"at": 0.0, "ok": None, "error": None}


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    svc = get_services(request)
    checks: dict[str, Any] = {}
    try:
        async with svc.sm() as s:
            await s.execute(text("select 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {type(e).__name__}"
    try:
        await svc.store.count()
        checks["qdrant"] = "ok"
    except Exception as e:
        checks["qdrant"] = f"error: {type(e).__name__}"
    if time.monotonic() - _google_cache["at"] > 300:
        try:
            from app.google.oauth import load_credentials

            load_credentials(svc.settings.google_token_file)
            _google_cache.update(ok=True, error=None)
        except Exception as e:
            _google_cache.update(ok=False, error=type(e).__name__)
        _google_cache["at"] = time.monotonic()
    checks["google_token"] = "ok" if _google_cache["ok"] else f"error: {_google_cache['error']}"
    ok = all(v == "ok" for v in checks.values())
    response.status_code = 200 if ok else 503
    return {"status": "ok" if ok else "degraded", "checks": checks}


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    svc = get_services(request)
    peer = request.client.host if request.client else ""
    try:
        addr = ipaddress.ip_address(peer)
        allowed = any(
            addr in ipaddress.ip_network(c, strict=False) for c in svc.config.security.metrics_allow_cidrs
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise HTTPException(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
