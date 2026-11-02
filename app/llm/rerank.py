"""Rerank qua POST {base}/rerank (qwen3-rerank). Lỗi ⇒ trả None để retriever dùng kết quả RRF."""

from __future__ import annotations

from typing import Protocol

import httpx

from app.config import RerankCfg
from app.logging import get_logger

log = get_logger(__name__)


class Reranker(Protocol):
    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]] | None: ...


class HttpReranker:
    def __init__(self, api_key: str, base_url: str, cfg: RerankCfg) -> None:
        self.url = base_url.rstrip("/") + "/rerank"
        self.cfg = cfg
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(timeout=cfg.timeout_s)

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None:
        if not documents:
            return []
        try:
            r = await self._client.post(
                self.url,
                headers=self._headers,
                json={
                    "model": self.cfg.model,
                    "query": query,
                    "documents": documents,
                    "top_n": min(top_n, len(documents)),
                },
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("results") or data.get("data") or []
            out = [(int(x["index"]), float(x.get("relevance_score", x.get("score", 0.0)))) for x in results]
            return sorted(out, key=lambda x: -x[1])[:top_n]
        except Exception as e:
            log.warning("rerank_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            return None

    async def aclose(self) -> None:
        await self._client.aclose()
