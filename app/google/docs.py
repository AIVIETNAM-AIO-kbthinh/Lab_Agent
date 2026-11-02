"""Docs client (P3 dùng cho biên bản; hiện chỉ đọc tài liệu)."""

from __future__ import annotations

from typing import Any, Protocol

from app.google.base import GoogleService


class DocsAPI(Protocol):
    async def get(self, document_id: str) -> dict[str, Any]: ...


class GoogleDocs:
    def __init__(self, creds: Any, timeout: float = 60) -> None:
        self._g = GoogleService("docs", "v1", creds, timeout)

    async def get(self, document_id: str) -> dict[str, Any]:
        res = await self._g.call(lambda s: s.documents().get(documentId=document_id))
        return dict(res)
