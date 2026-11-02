"""PDF converter giả: bytes "PDF" xác định từ docx (không cần LibreOffice khi test)."""

from __future__ import annotations

import hashlib


class FakePdf:
    def __init__(self, available: bool = True) -> None:
        self._available = available
        self.calls = 0

    @property
    def available(self) -> bool:
        return self._available

    async def to_pdf(self, docx: bytes) -> bytes:
        self.calls += 1
        return b"%PDF-1.4\n% fake\n" + hashlib.sha256(docx).hexdigest().encode() + b"\n%%EOF"
