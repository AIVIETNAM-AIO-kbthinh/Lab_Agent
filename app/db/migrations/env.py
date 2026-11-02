from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True).get("url")
    return x or context.config.attributes.get("url") or Settings().database_url


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Any) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
elif context.config.attributes.get("connection") is not None:
    # gọi từ code (app.db.migrate) với connection sync có sẵn
    _do_run(context.config.attributes["connection"])
else:
    from app.aio import run

    run(run_online())
