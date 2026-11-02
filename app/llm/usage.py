"""Chi phí LLM: ghi llm_usage, kiểm tra budget trước mỗi lời gọi (nguyên tắc production 6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import clock, metrics
from app.config import BudgetsCfg, ModelCap
from app.db.models import LLMUsage
from app.errors import BudgetExceeded
from app.llm.types import LLMResponse, Usage, UsageContext
from app.logging import get_logger

log = get_logger(__name__)


def cost_usd(cap: ModelCap, u: Usage) -> float:
    cr = cap.price_cache_read if cap.price_cache_read is not None else cap.price_in * 0.1
    cw = cap.price_cache_write if cap.price_cache_write is not None else cap.price_in * 1.25
    return (
        u.input_tokens * cap.price_in
        + u.output_tokens * cap.price_out
        + u.cache_read_tokens * cr
        + u.cache_write_tokens * cw
    ) / 1_000_000


def _start_of_today() -> datetime:
    now = clock.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_month() -> datetime:
    return _start_of_today().replace(day=1)


class UsageStore(Protocol):
    async def record(self, ctx: UsageContext, resp: LLMResponse, cost: float) -> None: ...
    async def tokens_since(
        self, since: datetime, *, session_public_id: str | None = None, account_id: str | None = None
    ) -> int: ...
    async def cost_since(self, since: datetime) -> float: ...


class DbUsageStore:
    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self.sm = sm

    async def record(self, ctx: UsageContext, resp: LLMResponse, cost: float) -> None:
        u = resp.usage
        async with self.sm() as s, s.begin():
            s.add(
                LLMUsage(
                    session_public_id=ctx.session_public_id,
                    account_id=ctx.account_id,
                    profile=ctx.profile,
                    model=resp.model,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_write_tokens=u.cache_write_tokens,
                    cost_usd=cost,
                    latency_ms=resp.latency_ms,
                    first_token_ms=resp.first_token_ms,
                    request_id=resp.request_id or ctx.request_id,
                    degraded=resp.degraded,
                )
            )

    async def tokens_since(
        self, since: datetime, *, session_public_id: str | None = None, account_id: str | None = None
    ) -> int:
        q = select(
            func.coalesce(
                func.sum(
                    LLMUsage.input_tokens
                    + LLMUsage.output_tokens
                    + LLMUsage.cache_read_tokens
                    + LLMUsage.cache_write_tokens
                ),
                0,
            )
        ).where(LLMUsage.ts >= since)
        if session_public_id:
            q = q.where(LLMUsage.session_public_id == session_public_id)
        if account_id:
            q = q.where(LLMUsage.account_id == account_id)
        async with self.sm() as s:
            return int(await s.scalar(q) or 0)

    async def cost_since(self, since: datetime) -> float:
        async with self.sm() as s:
            return float(
                await s.scalar(
                    select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(LLMUsage.ts >= since)
                )
                or 0.0
            )


class MemoryUsageStore:
    def __init__(self) -> None:
        self.rows: list[tuple[datetime, UsageContext, LLMResponse, float]] = []

    async def record(self, ctx: UsageContext, resp: LLMResponse, cost: float) -> None:
        self.rows.append((clock.now(), ctx, resp, cost))

    async def tokens_since(
        self, since: datetime, *, session_public_id: str | None = None, account_id: str | None = None
    ) -> int:
        return sum(
            r.usage.total
            for ts, c, r, _ in self.rows
            if ts >= since
            and (session_public_id is None or c.session_public_id == session_public_id)
            and (account_id is None or c.account_id == account_id)
        )

    async def cost_since(self, since: datetime) -> float:
        return sum(cost for ts, _, _, cost in self.rows if ts >= since)


@dataclass
class BudgetStatus:
    month_cost: float
    alert: bool


class BudgetGuard:
    def __init__(self, store: UsageStore, budgets: BudgetsCfg) -> None:
        self.store = store
        self.b = budgets

    async def check(
        self, *, guest: bool, session_public_id: str | None, account_id: str | None
    ) -> BudgetStatus:
        month = await self.store.cost_since(_start_of_month())
        if month >= self.b.lab_usd_per_month_hard_stop:
            log.error("budget_hard_stop", month_cost=round(month, 4))
            raise BudgetExceeded(
                "lab hard stop",
                user_message="Trợ lý AI tạm dừng do đã đạt hạn mức chi phí "
                "tháng này của lab. Vui lòng liên hệ admin.",
            )
        today = _start_of_today()
        if guest:
            used = await self.store.tokens_since(today, session_public_id=session_public_id)
            if used >= self.b.guest_tokens_per_session_per_day:
                raise BudgetExceeded("guest session budget")
        elif account_id:
            used = await self.store.tokens_since(today, account_id=account_id)
            if used >= self.b.member_tokens_per_user_per_day:
                raise BudgetExceeded("member daily budget")
        return BudgetStatus(month, month >= self.b.lab_usd_per_month_alert)


def observe(ctx: UsageContext, resp: LLMResponse, cost: float) -> None:
    u = resp.usage
    for kind, n in (
        ("input", u.input_tokens),
        ("output", u.output_tokens),
        ("cache_read", u.cache_read_tokens),
        ("cache_write", u.cache_write_tokens),
    ):
        if n:
            metrics.LLM_TOKENS.labels(ctx.profile, resp.model, kind).inc(n)
    metrics.LLM_COST.labels(ctx.profile, resp.model).inc(cost)
    if resp.first_token_ms is not None:
        metrics.CHAT_FIRST_TOKEN.labels(ctx.profile).observe(resp.first_token_ms / 1000)


def month_window() -> tuple[datetime, datetime]:
    s = _start_of_month()
    return s, (s + timedelta(days=32)).replace(day=1)
