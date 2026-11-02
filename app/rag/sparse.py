"""Sparse vector kiểu BM25 cho tiếng Việt: tách âm tiết + bigram âm tiết, hash về chỉ số; Qdrant tính IDF.

Bigram bắt được từ ghép ("máy chiếu"). Thêm biến thể bỏ dấu để câu hỏi gõ không dấu vẫn khớp.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import unicodedata
from collections import Counter

from app.domain.names import strip_accents

_TOKEN = re.compile(r"\w+", re.UNICODE)
K1 = 1.2


def tokenize(text: str) -> list[str]:
    t = unicodedata.normalize("NFC", text or "").lower()
    return [w for w in _TOKEN.findall(t) if not w.isdigit() or len(w) <= 6]


def _terms(text: str) -> list[str]:
    toks = tokenize(text)
    terms = list(toks)
    terms += [f"{a}_{b}" for a, b in itertools.pairwise(toks)]
    folded = [strip_accents(w) for w in toks]
    terms += [f"~{f}" for f, w in zip(folded, toks, strict=True) if f != w]
    terms += [f"~{a}_{b}" for a, b in itertools.pairwise(folded)]
    return terms


def _idx(term: str) -> int:
    return int.from_bytes(hashlib.blake2b(term.encode("utf-8"), digest_size=4).digest(), "big")


def _vector(counts: Counter[str], saturate: bool) -> tuple[list[int], list[float]]:
    acc: dict[int, float] = {}
    for term, tf in counts.items():
        w = tf * (K1 + 1) / (tf + K1) if saturate else 1.0
        i = _idx(term)
        acc[i] = acc.get(i, 0.0) + w
    items = sorted(acc.items())
    return [i for i, _ in items], [v for _, v in items]


def doc_vector(text: str) -> tuple[list[int], list[float]]:
    return _vector(Counter(_terms(text)), saturate=True)


def query_vector(text: str) -> tuple[list[int], list[float]]:
    return _vector(Counter(set(_terms(text))), saturate=False)
