"""Embedding qua endpoint OpenAI-compatible. Đổi model/số chiều ⇒ reindex toàn bộ."""

from __future__ import annotations

from typing import Protocol

import openai
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from app.config import EmbeddingCfg
from app.errors import ExternalServiceError


class Embedder(Protocol):
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, api_key: str, base_url: str, cfg: EmbeddingCfg, timeout_s: float = 60) -> None:
        self.client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=2)
        self.cfg = cfg
        self.dimensions = cfg.dimensions
        self.tokens_used = 0

    @retry(
        retry=retry_if_exception_type(
            (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_random_exponential(multiplier=1, max=20),
        reraise=True,
    )
    async def _batch(self, batch: list[str]) -> list[list[float]]:
        res = await self.client.embeddings.create(
            model=self.cfg.model, input=batch, dimensions=self.cfg.dimensions
        )
        if res.usage:
            self.tokens_used += int(res.usage.total_tokens or 0)
        vecs = [d.embedding for d in sorted(res.data, key=lambda d: d.index)]
        if any(len(v) != self.cfg.dimensions for v in vecs):
            raise ExternalServiceError(f"Embedding trả về sai số chiều (mong đợi {self.cfg.dimensions})")
        return vecs

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        bs = max(1, self.cfg.batch_size)
        for i in range(0, len(texts), bs):
            out.extend(await self._batch([t if t.strip() else " " for t in texts[i : i + bs]]))
        return out
