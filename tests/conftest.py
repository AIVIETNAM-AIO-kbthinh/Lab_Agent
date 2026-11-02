from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import aio, clock
from app.config import AdminCfg, LoadedConfig, Settings, load_config
from app.db.migrate import upgrade
from app.db.session import make_engine, make_sessionmaker
from app.llm.client import LLMClient
from app.llm.usage import DbUsageStore
from app.services import GoogleBundle, Services
from tests.factories import LabFixture
from tests.fakes.drive import FakeGmail
from tests.fakes.llm import FakeAdapter, FakeEmbedder, MemoryStore
from tests.fakes.render import FakePdf

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 16)  # thứ 6


def pytest_configure(config: Any) -> None:
    import io

    import structlog

    # log JSON gọn vào bộ nhớ: không in traceback rich, không phụ thuộc encoding console
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.JSONRenderer(ensure_ascii=True)],
        logger_factory=structlog.PrintLoggerFactory(file=io.StringIO()),
        cache_logger_on_first_use=False,
    )


def pytest_asyncio_loop_factories(config: Any, item: Any) -> dict[str, Any]:
    # psycopg async không chạy trên ProactorEventLoop (Windows)
    return {"selector": aio.loop_factory}


@pytest.fixture(autouse=True)
def frozen_clock() -> Iterator[None]:
    clock.configure("Asia/Ho_Chi_Minh")
    clock.set_now(lambda: datetime(2026, 10, 16, 9, 0, tzinfo=clock.tz()))
    yield
    clock.set_now(None)


def make_loaded(tmp_path: Path | None = None, **overrides: Any) -> LoadedConfig:
    s = Settings(
        _env_file=None,
        app_env="test",
        app_secret_key="x" * 48,
        llm_api_key="test",  # type: ignore[call-arg]
        database_url=os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://x:y@localhost/z"),
        **overrides,
    )
    loaded = load_config(s, ROOT / "config.example.yaml")
    cfg = loaded.config
    cfg.auth.admins = [AdminCfg(email="admin@gmail.com", ho_ten="Thầy Admin")]
    cfg.drive.root_folder_id = "ROOT"
    cfg.embedding.dimensions = 64
    if tmp_path is not None:
        cfg.paths.var_dir = str(tmp_path / "var")
    return loaded


@pytest.fixture
def loaded(tmp_path: Path) -> LoadedConfig:
    return make_loaded(tmp_path)


# ---------------------------------------------------------------- Postgres (testcontainers)


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - bản testcontainers cũ
        from testcontainers.postgres import PostgresContainer
    try:
        container = PostgresContainer("postgres:16-alpine", driver="psycopg")
        container.start()
    except Exception as e:  # Docker không chạy
        pytest.skip(f"Không chạy được Postgres container: {e}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(pg_url, pool_size=5)
    await upgrade(eng)
    yield eng
    await eng.dispose()


TABLES = [
    "messages",
    "conversations",
    "llm_usage",
    "outbox_steps",
    "outbox_ops",
    "idempotency_keys",
    "sessions",
    "accounts",
    "rate_limits",
    "uploads",
    "drafts",
    "pending_actions",
    "form_versions",
    "template_drafts",
    "exports",
    "sequences",
    "drive_state",
    "drive_path_cache",
    "index_docs",
    "web_pages",
    "web_crawl_runs",
]


@pytest.fixture
async def sm(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    yield make_sessionmaker(engine)


# ---------------------------------------------------------------- Services với bản giả


class Env:
    def __init__(
        self,
        svc: Services,
        lab: LabFixture,
        gmail: FakeGmail,
        anthropic: FakeAdapter,
        openai: FakeAdapter,
        store: MemoryStore,
        embedder: FakeEmbedder,
    ) -> None:
        self.svc, self.lab, self.gmail = svc, lab, gmail
        self.anthropic, self.openai, self.store, self.embedder = anthropic, openai, store, embedder


def build_env(loaded: LoadedConfig, sm: async_sessionmaker[AsyncSession], today: date = TODAY) -> Env:
    import app.tools.builtin  # noqa: F401

    lab = LabFixture(today)
    gmail = FakeGmail()
    a, o = FakeAdapter("anthropic"), FakeAdapter("openai_compat")
    store, emb = MemoryStore(), FakeEmbedder(loaded.config.embedding.dimensions)
    usage = DbUsageStore(sm)
    svc = Services(
        settings=loaded.settings,
        config=loaded.config,
        sm=sm,
        google=GoogleBundle(lab.drive, lab.sheets, gmail),
        llm=LLMClient(loaded.config.llm, {"anthropic": a, "openai_compat": o}, usage),
        usage=usage,
        embedder=emb,
        reranker=None,
        store=store,
        converter=FakePdf(),
    )
    return Env(svc, lab, gmail, a, o, store, emb)


@pytest.fixture
def env(loaded: LoadedConfig, sm: async_sessionmaker[AsyncSession]) -> Env:
    return build_env(loaded, sm)
