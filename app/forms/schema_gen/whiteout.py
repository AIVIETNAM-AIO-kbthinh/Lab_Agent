"""Tẩy trắng đơn có thông tin mẫu: thay phần thông tin riêng (do LLM chỉ ra, code kiểm tra lại) bằng chuỗi "…".

- Thông tin trên cùng dòng với nhãn ("Họ và tên: Nguyễn Văn A") → "Họ và tên: …………" (ô inline).
- Cả đoạn là thông tin (câu trả lời dưới mục "1. Hướng nghiên cứu") → dòng chấm (ô next_line).
- Ô bảng đã điền → để trống (ô bảng / bảng nhiều dòng).
- Luật cố định: "ngày 15 tháng 10 năm 2026" → "ngày … tháng … năm …"; ô đã tick ☒ → ☐.

Không sửa file gốc; trả bytes docx mới + báo cáo những gì đã xoá (hiển thị cho admin kiểm tra).
"""

from __future__ import annotations

import io
import re
from collections import defaultdict
from dataclasses import dataclass, field

from docx import Document

from app.forms.placeholder import BOXES, nfc, ptext, replace_span, set_paragraph_text
from app.forms.schema_gen.blanks import walk
from app.forms.schema_gen.llm_sweep import SampleSpan

FILL = "…" * 20
LINE = "…" * 60
DATE_FILLED = re.compile(r"ngày\s*(\d{1,2})\s*tháng\s*(\d{1,2})\s*năm\s*(\d{4})", re.IGNORECASE)
TICKED_BOXES = "☒☑"  # chỉ ô tick dạng hộp; ✓/■ có thể là ký hiệu in sẵn của mẫu
BOX_X = re.compile(rf"[{BOXES}]\s*[xX✓✔](?![^\W\d_])")  # "☐ x" / "☐✓": người điền đánh dấu cạnh ô
DATE_SLASH_FILLED = re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})(?![\d/])")


@dataclass
class WhiteoutReport:
    wiped: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)


def _locate(raw: str, text: str) -> tuple[int, int] | None:
    """Vị trí `text` trong đoạn (ưu tiên phần sau dấu ':' đầu tiên, để không đụng vào nhãn)."""
    text = text.strip()
    if not text:
        return None
    colon = raw.find(":")
    starts = [colon + 1, 0] if colon >= 0 else [0]
    for variant in dict.fromkeys([text, nfc(text)]):
        for s in starts:
            i = raw.find(variant, s)
            if i >= 0:
                return i, i + len(variant)
    words = [re.escape(w) for w in nfc(text).split()]
    if not words:
        return None
    m = re.search(r"\s+".join(words), nfc(raw))
    if m and len(nfc(raw)) == len(raw):
        return m.start(), m.end()
    return None


def whiteout(data: bytes, spans: list[SampleSpan], *, filled: bool = True) -> tuple[bytes, WhiteoutReport]:
    doc = Document(io.BytesIO(data))
    paras = walk(doc)
    by_idx = {pa.idx: pa for pa in paras}
    rep = WhiteoutReport()
    todo: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for sp in spans:
        try:
            idx = int(sp.para.strip().lstrip("pP"))
        except ValueError:
            rep.not_found.append(f"{sp.para}: '{sp.text[:60]}'")
            continue
        pa = by_idx.get(idx)
        loc = _locate(ptext(pa.p), sp.text) if pa is not None else None
        if pa is None or loc is None:
            rep.not_found.append(f"{sp.para}: '{sp.text[:60]}'")
            continue
        todo[idx].append((*loc, sp.text.strip()))

    for idx, items in todo.items():
        pa = by_idx[idx]
        raw = ptext(pa.p)
        items.sort(key=lambda x: x[0])
        merged: list[tuple[int, int, str]] = []
        for a, b, t in items:  # bỏ khoảng chồng nhau
            if merged and a < merged[-1][1]:
                continue
            merged.append((a, b, t))
        a0, b0, _ = merged[0]
        if len(merged) == 1 and not raw[:a0].strip() and not raw[b0:].strip().strip(".;,"):
            # cả đoạn là thông tin mẫu: ngoài bảng → dòng chấm (ô dưới nhãn), trong bảng → ô trống
            set_paragraph_text(pa.p, "" if pa.table is not None else LINE)
        else:
            for a, b, t in reversed(merged):
                fill = "…" if re.fullmatch(r"\d{1,4}", t) else FILL  # số ngắn (ngày, tháng, số lượng): ô ngắn
                replace_span(pa.p, a, b, (" " if a > 0 and raw[a - 1] == ":" else "") + fill)
        rep.wiped += [f"p{idx}: '{t[:60]}'" for _, _, t in merged]

    if filled:
        for pa in walk(doc):
            raw = ptext(pa.p)
            hits: list[tuple[int, int]] = []
            for rx in (DATE_FILLED, DATE_SLASH_FILLED):
                for m in rx.finditer(raw):
                    hits += [(m.start(g), m.end(g)) for g in (1, 2, 3)]
            hits += [(i, i + 1) for i, ch in enumerate(raw) if ch in TICKED_BOXES]
            hits += [(m.start(), m.end()) for m in BOX_X.finditer(raw)]
            if not hits:
                continue
            for a, b in sorted(set(hits), reverse=True):
                new = "☐" if raw[a] in TICKED_BOXES + BOXES else "…"
                replace_span(pa.p, a, b, new)
            rep.wiped.append(f"p{pa.idx}: ngày / ô tick")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), rep
