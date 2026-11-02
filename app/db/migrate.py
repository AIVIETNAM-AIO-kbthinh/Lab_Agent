"""Chạy Alembic từ code (CLI `labagent db upgrade`, test)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    return cfg


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    cfg = alembic_config()

    def _run(conn: Any) -> None:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)

    async with engine.begin() as conn:
        await conn.run_sync(_run)


def head_revision() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def current_revision(sm: async_sessionmaker[AsyncSession]) -> str | None:
    async with sm() as s:
        try:
            return (await s.execute(text("select version_num from alembic_version"))).scalar()
        except Exception:
            return None


async def assert_up_to_date(sm: async_sessionmaker[AsyncSession]) -> None:
    """Dừng tiến trình ngay khi DB chưa chạy migration mới nhất (thay vì lỗi khó hiểu giữa chừng)."""
    from app.errors import ConfigError

    rev, head = await current_revision(sm), head_revision()
    if rev != head:
        raise ConfigError(
            f"DB đang ở migration {rev}, code cần {head}: chạy `labagent db upgrade` rồi khởi động lại."
        )
