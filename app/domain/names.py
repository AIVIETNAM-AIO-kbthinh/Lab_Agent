"""Chuẩn hoá họ tên, SĐT, email; khớp mờ tên."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from rapidfuzz import fuzz, process


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def norm_name(s: str) -> str:
    """Khoá so khớp họ tên: NFC, gộp khoảng trắng, không phân biệt hoa thường."""
    return re.sub(r"\s+", " ", nfc(s)).strip().casefold()


def strip_accents(s: str) -> str:
    s = nfc(s).replace("đ", "d").replace("Đ", "D")  # NFD không tách được chữ đ
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def ascii_key(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s)).strip().casefold()


def to_pascal_ascii(s: str) -> str:
    """ "Nguyễn Văn Đức" -> "NguyenVanDuc"."""
    words = re.findall(r"[A-Za-z0-9]+", strip_accents(s))
    return "".join(w[:1].upper() + w[1:].lower() for w in words)


def normalize_phone(p: str) -> str:
    p = re.sub(r"[\s.\-()]", "", str(p or ""))
    if p.startswith("+84"):
        return "0" + p[3:]
    if p.startswith("84") and len(p) == 11:
        return "0" + p[2:]
    if re.fullmatch(r"\d{9}", p):  # sheet đọc số mất số 0 đầu
        return "0" + p
    return p


def valid_phone(p: str) -> bool:
    return re.fullmatch(r"0\d{9}", normalize_phone(p)) is not None


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def valid_email(e: str, allowed_domains: Iterable[str] | None = None) -> bool:
    m = _EMAIL.fullmatch((e or "").strip())
    if not m:
        return False
    if allowed_domains is None:
        return True
    return m.group(1).lower() in {d.lower() for d in allowed_domains}


def folder_name(ho_ten: str, sdt: str) -> str:
    """ "Nguyễn Văn Đức", "+84912345678" -> "NguyenVanDuc_0912345678"."""
    return f"{to_pascal_ascii(ho_ten)}_{normalize_phone(sdt)}"


def fuzzy_match(
    query: str, candidates: Iterable[str], *, limit: int = 5, cutoff: float = 70.0
) -> list[tuple[str, float]]:
    """Khớp mờ trên chuỗi bỏ dấu. Trùng khớp chính xác (sau chuẩn hoá) luôn đứng đầu với điểm 100."""
    cands = list(dict.fromkeys(candidates))
    q = ascii_key(query)
    if not q:
        return []
    exact = [c for c in cands if norm_name(c) == norm_name(query)]
    if exact:
        return [(exact[0], 100.0)]
    keyed = {c: ascii_key(c) for c in cands}
    res = process.extract(q, keyed, scorer=fuzz.WRatio, limit=limit, score_cutoff=cutoff)
    # bổ sung: query là một phần của tên ("Trung" khớp "Đào Hoàng Trung")
    out: dict[str, float] = {}
    for _choice, score, key in res:
        out[key] = float(score)
    for c, k in keyed.items():
        if q in k.split() or (len(q) >= 3 and q in k):
            out[c] = max(out.get(c, 0.0), 90.0)
    return sorted(out.items(), key=lambda x: (-x[1], x[0]))[:limit]
