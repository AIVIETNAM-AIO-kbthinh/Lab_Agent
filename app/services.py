"""Gom dependency của tiến trình (api / worker / cli). Test dựng Services với bản giả."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import AppConfig, LoadedConfig, Settings
from app.db.session import make_engine, make_sessionmaker
from app.domain.audit import AuditLog
from app.domain.duty import DutyRepo
from app.domain.equipment import EquipmentRepo
from app.domain.members import MemberDirectory
from app.domain.submissions import SubmissionsRepo
from app.forms.registry import FormRegistry
from app.forms.render import PdfConverter, Renderer, SofficeConverter, TemplateStore
from app.google.drive import DriveAPI
from app.google.gmail import GmailAPI
from app.google.paths import DbPathCache, PathCache, PathResolver
from app.google.sheets import SheetsAPI
from app.llm.client import Adapter, LLMClient
from app.llm.embeddings import Embedder
from app.llm.rerank import Reranker
from app.llm.usage import BudgetGuard, DbUsageStore, UsageStore
from app.mail.mailer import Mailer
from app.rag.indexer import Indexer
from app.rag.retriever import Retriever
from app.rag.store import VectorStore
from app.tools.registry import REGISTRY, ToolRegistry


@dataclass
class GoogleBundle:
    drive: DriveAPI
    sheets: SheetsAPI
    gmail: GmailAPI | None
    docs: Any = None


@dataclass
class Services:
    settings: Settings
    config: AppConfig
    sm: async_sessionmaker[AsyncSession]
    google: GoogleBundle
    llm: LLMClient
    usage: UsageStore
    embedder: Embedder
    reranker: Reranker | None
    store: VectorStore
    registry: ToolRegistry = field(default_factory=lambda: REGISTRY)
    engine: AsyncEngine | None = None
    path_cache: PathCache | None = None
    converter: PdfConverter | None = None  # None = LibreOffice (SofficeConverter); test tiêm bản giả

    def __post_init__(self) -> None:
        cfg = self.config
        g = self.google
        self.paths = PathResolver(g.drive, cfg.drive.root_folder_id, self.path_cache)
        self.members = MemberDirectory(g.sheets, self.paths, cfg.sheets.members, cfg.auth.role_cache_seconds)
        self.equipment = EquipmentRepo(g.sheets, self.paths, cfg.sheets.equipment)
        self.duty = DutyRepo(g.sheets, self.paths, cfg.sheets.duty)
        self.submissions = SubmissionsRepo(g.sheets, self.paths, cfg.sheets.form_submissions)
        self.audit = AuditLog(g.sheets, self.paths, cfg.sheets.audit_log)
        self.mailer = Mailer(g.gmail, cfg)
        self.budget = BudgetGuard(self.usage, cfg.llm.budgets)
        self.indexer = Indexer(self.store, self.embedder, self.sm, cfg.index, cfg.qdrant_collection)
        self.retriever = Retriever(self.store, self.embedder, self.reranker, cfg.rerank)
        self.forms = FormRegistry(self.sm)
        self.renderer = Renderer(self.converter or SofficeConverter(cfg.render))
        self.templates = TemplateStore(g.drive, cfg.var_dir / "templates")

    async def aclose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


class LazyGoogle:
    """Chỉ đọc token Google khi lần đầu cần (CLI quản trị tài khoản không cần Google)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._bundle: GoogleBundle | None = None

    def get(self) -> GoogleBundle:
        if self._bundle is None:
            from app.google.docs import GoogleDocs
            from app.google.drive import GoogleDrive
            from app.google.gmail import GoogleGmail
            from app.google.oauth import load_credentials
            from app.google.sheets import GoogleSheets

            creds = load_credentials(self.settings.google_token_file)
            self._bundle = GoogleBundle(
                GoogleDrive(creds), GoogleSheets(creds), GoogleGmail(creds), GoogleDocs(creds)
            )
        return self._bundle


class _LazyProxy:
    def __init__(self, lazy: LazyGoogle, attr: str) -> None:
        self._lazy = lazy
        self._attr = attr

    def __getattr__(self, name: str) -> Any:
        return getattr(getattr(self._lazy.get(), self._attr), name)


def build_services(loaded: LoadedConfig, *, pool_size: int = 5) -> Services:
    from app.llm.anthropic_adapter import AnthropicAdapter
    from app.llm.embeddings import OpenAIEmbedder
    from app.llm.openai_adapter import OpenAICompatAdapter
    from app.llm.rerank import HttpReranker
    from app.rag.store import QdrantStore

    s, cfg = loaded.settings, loaded.config
    engine = make_engine(s.database_url, pool_size=pool_size)
    sm = make_sessionmaker(engine)
    key = s.llm_api_key.get_secret_value()
    adapters: dict[str, Adapter] = {
        "anthropic": AnthropicAdapter(key, s.llm_anthropic_base_url, cfg.llm.request_timeout_s),
        "openai_compat": OpenAICompatAdapter(key, s.llm_openai_base_url, cfg.llm.request_timeout_s),
    }
    usage = DbUsageStore(sm)
    lazy = LazyGoogle(s)
    google = GoogleBundle(
        _LazyProxy(lazy, "drive"),
        _LazyProxy(lazy, "sheets"),
        _LazyProxy(lazy, "gmail"),
        _LazyProxy(lazy, "docs"),
    )
    store = QdrantStore(
        s.qdrant_url,
        s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else None,
        cfg.qdrant_collection,
        cfg.qdrant_alias,
        cfg.embedding.dimensions,
    )
    import app.tools.builtin  # noqa: F401  (đăng ký tool vào REGISTRY)

    return Services(
        settings=s,
        config=cfg,
        sm=sm,
        google=google,
        llm=LLMClient(cfg.llm, adapters, usage),
        usage=usage,
        embedder=OpenAIEmbedder(key, s.llm_openai_base_url, cfg.embedding),
        reranker=HttpReranker(key, s.llm_openai_base_url, cfg.rerank) if cfg.rerank.enabled else None,
        store=store,
        engine=engine,
        path_cache=DbPathCache(sm),
    )
