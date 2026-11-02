"""Hybrid retrieval: dense + sparse, gộp RRF, rerank top-k.

Bộ lọc `access` dựng từ ROLE ngay trong retriever; LLM không truyền được. Guest chỉ thấy `public`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.auth.roles import Role
from app.config import RerankCfg
from app.llm.embeddings import Embedder
from app.llm.rerank import Reranker
from app.logging import get_logger
from app.rag.indexer import EPOCH
from app.rag.sparse import query_vector
from app.rag.store import Hit, SearchFilter, VectorStore

log = get_logger(__name__)


def allowed_access(role: Role) -> list[str]:
    return ["public"] if role == Role.GUEST else ["public", "member"]


@dataclass
class SearchResult:
    hits: list[Hit]
    reranked: bool


class Retriever:
    def __init__(
        self, store: VectorStore, embedder: Embedder, reranker: Reranker | None, cfg: RerankCfg
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.cfg = cfg

    async def search(
        self,
        query: str,
        role: Role,
        *,
        doc_type: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        top_n: int | None = None,
    ) -> SearchResult:
        allowed = allowed_access(role)
        flt = SearchFilter(
            access_in=allowed,
            doc_type=doc_type,
            day_from=(date_from - EPOCH).days if date_from else None,
            day_to=(date_to - EPOCH).days if date_to else None,
        )
        n = top_n or self.cfg.top_n
        dense = (await self.embedder.embed([query]))[0]
        cands = await self.store.hybrid(
            dense, query_vector(query), flt, prefetch=self.cfg.candidates, limit=self.cfg.candidates
        )
        # phòng thủ nhiều lớp: lọc lại theo payload dù filter đã áp dụng ở Qdrant
        cands = [h for h in cands if h.payload.get("access") in allowed]
        if not cands:
            return SearchResult([], False)
        if self.cfg.enabled and self.reranker is not None and len(cands) > 1:
            ranked = await self.reranker.rerank(query, [h.text for h in cands], n)
            if ranked is not None:
                out = []
                for idx, score in ranked:
                    if 0 <= idx < len(cands):
                        h = cands[idx]
                        out.append(Hit(h.id, score, h.payload))
                return SearchResult(out[:n], True)
            log.info("rerank_fallback_rrf", candidates=len(cands))
        return SearchResult(cands[:n], False)
