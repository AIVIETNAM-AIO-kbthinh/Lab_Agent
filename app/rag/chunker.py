"""Chia markdown theo heading, ~chunk_tokens token, overlap ~12%.

Mỗi chunk có tiền tố "<tiêu đề tài liệu> › <tiêu đề mục>". Các mục nhỏ liền nhau (vd từng thẻ thành viên)
được gộp vào cùng chunk để không sinh hàng trăm chunk vụn.

Nhãn quyền trong heading ("[Nội bộ]", "[Công khai]", "[Internal]", "[Public]") áp cho cả mục con bên dưới:
chunk không bao giờ gộp nội dung khác nhãn, và overlap không vượt qua ranh giới nhãn.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_TAG_MEMBER = re.compile(r"\[\s*(nội bộ|noi bo|internal|members? only)\s*\]", re.IGNORECASE)
_TAG_PUBLIC = re.compile(r"\[\s*(công khai|cong khai|public)\s*\]", re.IGNORECASE)


def heading_access(path: list[str]) -> str | None:
    """Nhãn quyền của heading sâu nhất có gắn nhãn; None = theo quyền của cả tài liệu."""
    for raw in reversed(path):
        h = unicodedata.normalize("NFC", raw)
        if _TAG_MEMBER.search(h):
            return "member"
        if _TAG_PUBLIC.search(h):
            return "public"
    return None


@lru_cache(maxsize=1)
def _encoder() -> object | None:
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # không tải được file BPE (máy offline) → ước lượng
        return None


def count_tokens(text: str) -> int:
    enc = _encoder()
    if enc is None:
        return max(1, int(len(text) / 3.2))
    return len(enc.encode(text, disallowed_special=()))  # type: ignore[attr-defined]


@dataclass
class Chunk:
    index: int
    text: str  # đã có tiền tố
    heading: str
    tokens: int
    access: str | None = None  # nhãn trong heading; None = theo tài liệu


@dataclass
class _Unit:
    path: list[str]
    level: int
    text: str

    @property
    def access(self) -> str | None:
        return heading_access(self.path)


def _sections(markdown: str) -> list[_Unit]:
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    cur_level = 0

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            units.append(_Unit([h for _, h in stack], cur_level, body))
        buf.clear()

    for line in markdown.split("\n"):
        m = _HEAD.match(line)
        if m:
            flush()
            level, text = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            cur_level = level
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return units


def _split_long(text: str, max_tokens: int) -> list[str]:
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    for p in paras:
        if count_tokens(p) <= max_tokens:
            out.append(p)
            continue
        sentences = re.split(r"(?<=[.!?…;])\s+", p)
        cur = ""
        for s in sentences:
            if count_tokens(s) > max_tokens:  # câu cực dài: cắt theo từ
                words = s.split()
                step = max(20, int(max_tokens * 2.5))
                pieces = [" ".join(words[i : i + step]) for i in range(0, len(words), step)]
            else:
                pieces = [s]
            for piece in pieces:
                cand = f"{cur} {piece}".strip()
                if cur and count_tokens(cand) > max_tokens:
                    out.append(cur)
                    cur = piece
                else:
                    cur = cand
        if cur:
            out.append(cur)
    return out


def _tail(text: str, tokens: int) -> str:
    if tokens <= 0:
        return ""
    words = text.split()
    acc: list[str] = []
    for w in reversed(words):
        acc.append(w)
        if count_tokens(" ".join(acc)) >= tokens:
            break
    return " ".join(reversed(acc))


def chunk_markdown(title: str, markdown: str, chunk_tokens: int = 700, overlap: float = 0.12) -> list[Chunk]:
    units = _sections(markdown)
    if not units:
        return []
    ov_tokens = int(chunk_tokens * overlap)
    pieces: list[tuple[str, str, str | None]] = []  # (heading, text, access)
    cur_text: list[str] = []
    cur_heading = ""
    cur_top: str | None = None
    cur_access: str | None = None
    cur_tok = 0

    def top_of(u: _Unit) -> str:
        return u.path[0] if u.path else ""

    def emit() -> None:
        nonlocal cur_text, cur_tok
        if cur_text:
            pieces.append((cur_heading, "\n\n".join(cur_text), cur_access))
        cur_text, cur_tok = [], 0

    for u in units:
        parts = _split_long(u.text, chunk_tokens) if count_tokens(u.text) > chunk_tokens else [u.text]
        heading = " › ".join(u.path[:2]) if u.path else ""
        for part in parts:
            t = count_tokens(part)
            starts_chunk = not cur_text
            same_access = u.access == cur_access
            if cur_text and (cur_tok + t > chunk_tokens or top_of(u) != cur_top or not same_access):
                prev = cur_text[-1]
                emit()
                starts_chunk = True
                if top_of(u) == cur_top and same_access and ov_tokens:
                    tail = _tail(prev, ov_tokens)
                    if tail:
                        cur_text, cur_tok = [tail], count_tokens(tail)
            if starts_chunk:
                cur_heading = heading  # tiêu đề của mục mở đầu chunk (không phải của phần overlap)
            cur_top = top_of(u)
            cur_access = u.access
            cur_text.append(part)
            cur_tok += t
    emit()

    chunks: list[Chunk] = []
    for i, (heading, text, access) in enumerate(pieces):
        prefix = f"{title} › {heading}" if heading and heading != title else title
        full = f"{prefix}\n\n{text}" if prefix else text
        chunks.append(Chunk(i, full, heading, count_tokens(full), access))
    return chunks
