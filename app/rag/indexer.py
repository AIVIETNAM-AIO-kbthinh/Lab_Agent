"""Indexer dùng chung cho nguồn Drive và web: so hash → chèn chunk mới → xoá chunk cũ."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import metrics
from app.config import IndexCfg
from app.db.models import IndexDoc
from app.llm.embeddings import Embedder
from app.logging import get_logger
from app.rag.chunker import chunk_markdown
from app.rag.sparse import doc_vector
from app.rag.store import ChunkPoint, VectorStore

log = get_logger(__name__)
EPOCH = date(1970, 1, 1)
# tăng khi đổi cách chia chunk/gắn payload ⇒ mọi tài liệu được index lại ở lần sync kế tiếp
INDEX_VERSION = "2"


def chunk_access(doc_access: str, chunk_access: str | None) -> str:
    """Nhãn trong heading chỉ được HẠ quyền (public → member), không bao giờ mở tài liệu nội bộ cho khách."""
    if doc_access == "member" or chunk_access == "member":
        return "member"
    return "public"


@dataclass
class SourceDoc:
    doc_id: str
    source: Literal["drive", "web"]
    location: str  # path Drive hoặc URL
    title: str
    access: Literal["public", "member"]
    doc_type: str  # rule | minutes | web | other
    markdown: str
    lang: str | None = None
    doc_date: date | None = None
    source_version: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for part in (
            INDEX_VERSION,
            self.title,
            self.access,
            self.doc_type,
            self.lang or "",
            str(self.doc_date or ""),
            self.location,
            self.markdown,
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()


@dataclass
class IndexOutcome:
    doc_id: str
    status: Literal["indexed", "unchanged", "empty", "deleted"]
    chunks: int = 0


def doc_id_for(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class Indexer:
    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        sm: async_sessionmaker[AsyncSession],
        cfg: IndexCfg,
        collection: str,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.sm = sm
        self.cfg = cfg
        self.collection = collection

    async def known(self, source: str) -> dict[str, IndexDoc]:
        async with self.sm() as s:
            rows = (await s.scalars(select(IndexDoc).where(IndexDoc.source == source))).all()
            return {r.doc_id: r for r in rows}

    async def upsert_doc(self, doc: SourceDoc, *, force: bool = False) -> IndexOutcome:
        h = doc.content_hash()
        async with self.sm() as s:
            existing = await s.get(IndexDoc, doc.doc_id)
        if existing and existing.content_hash == h and not force:
            if doc.source_version and existing.source_version != doc.source_version:
                async with self.sm() as s, s.begin():
                    row = await s.get(IndexDoc, doc.doc_id)
                    if row:
                        row.source_version = doc.source_version
            return IndexOutcome(doc.doc_id, "unchanged", existing.chunk_count)

        chunks = chunk_markdown(doc.title, doc.markdown, self.cfg.chunk_tokens, self.cfg.chunk_overlap)
        if not chunks:
            if existing:
                await self.delete_doc(doc.doc_id)
            return IndexOutcome(doc.doc_id, "empty")
        vectors = await self.embedder.embed([c.text for c in chunks])
        day = (doc.doc_date - EPOCH).days if doc.doc_date else None
        points = [
            ChunkPoint(
                doc.doc_id,
                c.index,
                h,
                v,
                doc_vector(c.text),
                {
                    "text": c.text,
                    "title": doc.title,
                    "heading": c.heading,
                    "source": doc.source,
                    "location": doc.location,
                    "access": chunk_access(doc.access, c.access),
                    "doc_type": doc.doc_type,
                    "lang": doc.lang or "und",
                    "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
                    "doc_day": day,
                },
            )
            for c, v in zip(chunks, vectors, strict=True)
        ]
        await self.store.upsert(points)
        await self.store.delete_doc(doc.doc_id, keep_hash=h)
        async with self.sm() as s, s.begin():
            row = await s.get(IndexDoc, doc.doc_id)
            if row is None:
                row = IndexDoc(doc_id=doc.doc_id)
                s.add(row)
            row.source, row.location, row.title = doc.source, doc.location[:1000], doc.title[:500]
            row.access, row.doc_type, row.lang, row.doc_date = (
                doc.access,
                doc.doc_type,
                doc.lang,
                doc.doc_date,
            )
            row.content_hash, row.source_version, row.chunk_count = h, doc.source_version, len(chunks)
            row.collection = self.collection
        metrics.INDEX_CHUNKS.labels(doc.source).inc(len(chunks))
        log.info("doc_indexed", doc_id=doc.doc_id, source=doc.source, chunks=len(chunks))
        return IndexOutcome(doc.doc_id, "indexed", len(chunks))

    async def delete_doc(self, doc_id: str) -> IndexOutcome:
        await self.store.delete_doc(doc_id)
        async with self.sm() as s, s.begin():
            await s.execute(delete(IndexDoc).where(IndexDoc.doc_id == doc_id))
        log.info("doc_deleted", doc_id=doc_id)
        return IndexOutcome(doc_id, "deleted")

    async def reconcile(self, source: str, live_ids: set[str]) -> list[str]:
        """Xoá các doc của `source` không còn trong `live_ids`."""
        stale = [d for d in (await self.known(source)) if d not in live_ids]
        for d in stale:
            await self.delete_doc(d)
        return stale
