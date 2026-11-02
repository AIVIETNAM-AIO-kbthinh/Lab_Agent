"""Chèn placeholder docxtpl vào BẢN SAO mẫu đơn theo `anchor` trong schema (TK §7.7 bước ④).

- Không sửa file gốc; trả về bytes docx mới + báo cáo trường nào đã/không chèn được.
- Thay thế theo vị trí ký tự trên các run của đoạn văn: placeholder nằm gọn trong MỘT run (docxtpl yêu cầu),
  định dạng của nhãn đứng trước được giữ nguyên.
- Dùng chung cho P3 (schema viết tay) và P4 (schema tự sinh).
"""

from __future__ import annotations

import copy
import io
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.table import Table, _Cell, _Row
from docx.text.paragraph import Paragraph

from app.forms.schema import FieldSpec, FormSchema

DOTS = "….·_"
# một "ô trống": chuỗi ≥2 dấu chấm/…/gạch dưới, hoặc một dấu "…"
HOLE = r"(?:[.…_]{2,}|…)"
# ô trống kéo dài, kể cả dạng ngày "…/…/……"
HOLE_RUN = rf"{HOLE}(?:[\s/\-]*{HOLE})*"
_DOT_RUN = re.compile(rf"{HOLE_RUN}[\s]*")
_DOTS_ONLY = re.compile(r"^[\s.…_]{3,}$")
BOXES = "☐□❑"  # ô tick trống
TICKED = "☒☑✓✔■▣"  # ô đã tick


@dataclass
class PlaceholderReport:
    placed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # "<key>: lý do"
    signatures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def anchor_regex(anchor: str) -> re.Pattern[str]:
    """Anchor → regex chịu được khác biệt khoảng trắng và "…" so với "..."."""
    parts: list[str] = []
    for ch in re.sub(HOLE, "…", nfc(anchor)).strip():
        if ch.isspace():
            if not parts or parts[-1] != r"\s*":
                parts.append(r"\s*")
        elif ch == "…":
            parts.append(HOLE)
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.IGNORECASE)


# ---------------------------------------------------------------- duyệt tài liệu


def iter_blocks(container: Any) -> Iterator[Paragraph | Table]:
    """Đoạn văn và bảng theo đúng thứ tự trong tài liệu / ô bảng."""
    el = container.element.body if isinstance(container, DocxDocument) else container._element
    for ch in el.iterchildren():
        tag = ch.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(ch, container)
        elif tag == "tbl":
            yield Table(ch, container)


def iter_paragraphs(container: Any) -> Iterator[Paragraph]:
    """Mọi đoạn văn kể cả trong bảng (đệ quy), theo thứ tự."""
    for b in iter_blocks(container):
        if isinstance(b, Paragraph):
            yield b
        else:
            seen: list[Any] = []  # giữ tham chiếu phần tử để so `is` ổn định (ô gộp lặp lại)
            for row in b.rows:
                for cell in row.cells:
                    if any(tc is cell._tc for tc in seen):
                        continue
                    seen.append(cell._tc)
                    yield from iter_paragraphs(cell)


def ptext(p: Paragraph) -> str:
    return "".join(r.text for r in p.runs)


# ---------------------------------------------------------------- thao tác run


def replace_span(p: Paragraph, start: int, end: int, new: str) -> None:
    """Thay text [start, end) của đoạn văn bằng `new`; `new` nằm trong run chứa ký tự đầu của đoạn bị thay."""
    runs = p.runs
    if start == end:
        insert_at(p, start, new)
        return
    pos = 0
    placed = False
    for r in runs:
        t = r.text
        rs, re_ = pos, pos + len(t)
        pos = re_
        if re_ <= start or rs >= end:
            continue
        a, b = max(start, rs) - rs, min(end, re_) - rs
        if not placed:
            r.text = t[:a] + new + t[b:]
            placed = True
        else:
            r.text = t[:a] + t[b:]
    if not placed:
        insert_at(p, start, new)


def insert_at(p: Paragraph, at: int, new: str) -> None:
    runs = p.runs
    if not runs:
        p.add_run(new)
        return
    pos = 0
    containing = None
    for r in runs:
        t = r.text
        rs, re_ = pos, pos + len(t)
        pos = re_
        if rs == at and t:  # ưu tiên run bắt đầu đúng vị trí (giữ định dạng phần chữ đứng sau)
            r.text = new + t
            return
        if rs < at <= re_:
            containing = (r, at - rs)
    if containing is None:
        r = runs[-1]
        r.text = r.text + new
        return
    r, off = containing
    r.text = r.text[:off] + new + r.text[off:]


def set_paragraph_text(p: Paragraph, text: str) -> None:
    runs = p.runs
    if not runs:
        p.add_run(text)
        return
    runs[0].text = text
    for r in runs[1:]:
        r._r.getparent().remove(r._r)


def set_cell_text(cell: _Cell, text: str) -> None:
    paras = cell.paragraphs
    set_paragraph_text(paras[0], text)
    for p in paras[1:]:
        p._p.getparent().remove(p._p)


def ph(name: str) -> str:
    return "{{ " + name + " }}"


# ---------------------------------------------------------------- chèn theo từng kiểu ô trống


def _find_anchors(doc: DocxDocument, anchor: str) -> list[tuple[Paragraph, re.Match[str]]]:
    """Mọi vị trí khớp anchor theo thứ tự tài liệu (mẫu đơn có thể lặp nhãn, vd hai dòng "Họ và tên:")."""
    rx = anchor_regex(anchor)
    return [(p, m) for p in iter_paragraphs(doc) for m in rx.finditer(nfc(ptext(p)))]


def cell_right_of(p: Paragraph) -> Any | None:
    """Phần tử w:tc ngay bên phải ô chứa đoạn văn `p` (cùng hàng), hoặc None nếu `p` không nằm trong bảng."""
    tc = p._p.getparent()
    if tc is None or not tc.tag.endswith("}tc"):
        return None
    nxt = tc.getnext()
    while nxt is not None and not nxt.tag.endswith("}tc"):
        nxt = nxt.getnext()
    return nxt


def _cell(p: Paragraph, key: str) -> bool:
    tc = cell_right_of(p)
    if tc is None:
        return False
    cell = _Cell(tc, None)  # type: ignore[arg-type]  # chỉ dùng paragraphs/text, không cần bảng cha
    if cell.text.strip():
        return False
    set_cell_text(cell, ph(key))
    return True


def checkbox_lines(doc: DocxDocument, p: Paragraph, m: re.Match[str]) -> list[tuple[Paragraph, int]]:
    """(đoạn văn, vị trí bắt đầu) chứa các ô tick của một trường: cùng dòng sau anchor, hoặc các dòng kế tiếp
    bắt đầu bằng ô tick."""
    text = nfc(ptext(p))
    if any(ch in BOXES + TICKED for ch in text[m.end() :]):
        return [(p, m.end())]
    out: list[tuple[Paragraph, int]] = []
    found = False
    for q in iter_paragraphs(doc):
        if found:
            t = nfc(ptext(q)).lstrip()
            if not t:
                continue
            if t[0] in BOXES + TICKED:
                out.append((q, 0))
                continue
            break
        if q._p is p._p:
            found = True
    return out


def _checkbox(doc: DocxDocument, p: Paragraph, m: re.Match[str], f: FieldSpec) -> bool:
    lines = checkbox_lines(doc, p, m)
    boxes = [(q, i) for q, start in lines for i, ch in enumerate(nfc(ptext(q))) if i >= start and ch in BOXES]
    if len(boxes) != len(f.options):
        return False
    for n, (q, i) in reversed(list(enumerate(boxes))):
        replace_span(q, i, i + 1, ph(f"{f.key}_opt{n}"))
    return True


def _inline(p: Paragraph, m: re.Match[str], key: str) -> bool:
    text = nfc(ptext(p))
    dm = _DOT_RUN.search(text, m.end())
    if dm is None or text[m.end() : dm.start()].strip():
        return False
    start, end = dm.start(), dm.end()
    # giữ khoảng trắng cuối nếu sau ô trống còn chữ
    tail = text[start:end]
    end = start + len(tail.rstrip()) if end < len(text) else end
    lead = "" if start > 0 and text[start - 1].isspace() else " "
    replace_span(p, start, end, lead + ph(key))
    return True


def _ellipsis(p: Paragraph, m: re.Match[str], key: str) -> bool:
    text = nfc(ptext(p))
    dm = re.compile(HOLE_RUN).search(text, m.end())
    if dm is None:
        return False
    replace_span(p, dm.start(), dm.end(), ph(key))
    return True


def _after_anchor(p: Paragraph, m: re.Match[str], key: str) -> bool:
    text = nfc(ptext(p))
    end = m.end()
    lead = "" if end < len(text) and text[end] == " " else " "
    # "…CV: " → chèn sau dấu cách sẵn có
    if end < len(text) and text[end] == " ":
        end += 1
        lead = ""
    insert_at(p, end, lead + ph(key))
    return True


def _date_parts(p: Paragraph, m: re.Match[str], key: str) -> bool:
    text = nfc(ptext(p))
    seg = text[m.start() : m.end()]
    holes = list(re.finditer(HOLE, seg))
    if len(holes) != 3:
        return False
    for suffix, h in reversed(list(zip(("d", "m", "y"), holes, strict=True))):
        replace_span(p, m.start() + h.start(), m.start() + h.end(), ph(f"{key}_{suffix}"))
    return True


def _next_line(doc: DocxDocument, p: Paragraph, key: str) -> bool:
    found = False
    for q in iter_paragraphs(doc):
        if found:
            t = nfc(ptext(q))
            if not t.strip():
                continue
            if _DOTS_ONLY.match(t):
                set_paragraph_text(q, ph(key))
                return True
            return False
        if q._p is p._p:
            found = True
    return False


def _table_after(doc: DocxDocument, p: Paragraph) -> Table | None:
    found = False
    for b in iter_blocks(doc):
        if isinstance(b, Paragraph) and b._p is p._p:
            found = True
        elif found and isinstance(b, Table):
            return b
    return None


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", nfc(s)).strip().casefold()


def _table(doc: DocxDocument, p: Paragraph, f: FieldSpec) -> str | None:
    """Chèn vòng lặp docxtpl vào bảng đầu tiên sau anchor. Trả lý do nếu không chèn được."""
    t = _table_after(doc, p)
    if t is None or len(t.rows) < 2:
        return f"{f.key}: không thấy bảng có dòng trống sau '{f.anchor}'"
    header = [_norm_label(c.text) for c in t.rows[0].cells]
    labels = [_norm_label(c.label) for c in f.columns]
    if header != labels:
        return f"{f.key}: tiêu đề bảng {header} khác columns {labels}"
    proto: _Row = t.rows[1]
    for extra in list(t.rows)[2:]:
        extra._tr.getparent().remove(extra._tr)
    for cell, col in zip(proto.cells, f.columns, strict=True):
        set_cell_text(cell, ph(f"r.{col.key}"))
    open_tr = copy.deepcopy(proto._tr)
    close_tr = copy.deepcopy(proto._tr)
    proto._tr.addprevious(open_tr)
    proto._tr.addnext(close_tr)
    for tr, tag in ((open_tr, "{%tr for r in " + f.key + " %}"), (close_tr, "{%tr endfor %}")):
        row = _Row(tr, t)
        for i, cell in enumerate(row.cells):
            set_cell_text(cell, tag if i == 0 else "")
    return None


def _signatures(doc: DocxDocument, schema: FormSchema, report: PlaceholderReport) -> None:
    tables = [b for b in iter_blocks(doc) if isinstance(b, Table)]
    for i, sig in enumerate(schema.signatures):
        if not sig.field:
            continue
        target = _norm_label(sig.label)
        placed = False
        for t in tables:
            rows = list(t.rows)
            for ri, row in enumerate(rows):
                cells = row.cells
                for ci, cell in enumerate(cells):
                    first = _norm_label(cell.paragraphs[0].text) if cell.paragraphs else ""
                    if not first.startswith(target):
                        continue
                    align = cell.paragraphs[0].alignment or WD_ALIGN_PARAGRAPH.CENTER
                    below = rows[ri + 1].cells[ci] if ri + 1 < len(rows) else None
                    if below is not None and not below.text.strip():
                        para = below.paragraphs[-1]
                        set_paragraph_text(para, ph(f"sig_{i}"))
                    else:
                        for _ in range(3):
                            cell.add_paragraph("")
                        para = cell.add_paragraph(ph(f"sig_{i}"))
                    para.alignment = align
                    placed = True
                    break
                if placed:
                    break
            if placed:
                break
        if placed:
            report.signatures.append(sig.label)
        else:
            report.missing.append(f"chữ ký '{sig.label}': không tìm thấy ô ký")


def _place(doc: DocxDocument, p: Paragraph, m: re.Match[str], f: FieldSpec) -> bool:
    if f.blank == "date_parts":
        return _date_parts(p, m, f.key)
    if f.blank == "next_line":
        return _next_line(doc, p, f.key)
    if f.blank == "after_anchor":
        return _after_anchor(p, m, f.key)
    if f.blank == "ellipsis":
        return _ellipsis(p, m, f.key)
    if f.blank == "cell":
        return _cell(p, f.key)
    if f.blank == "checkbox":
        return _checkbox(doc, p, m, f)
    return _inline(p, m, f.key)


def insert_placeholders(template: bytes, schema: FormSchema) -> tuple[bytes, PlaceholderReport]:
    doc = Document(io.BytesIO(template))
    report = PlaceholderReport()
    # vị trí anchor đã dùng (phần tử đoạn văn, vị trí): nhãn lặp lại ⇒ trường sau dùng lần xuất hiện kế tiếp
    used: list[tuple[Any, int]] = []
    for f in schema.fields:
        if not f.anchor or f.filled_by == "lab":
            continue
        hits = _find_anchors(doc, f.anchor)
        if not hits:
            report.missing.append(f"{f.key}: không tìm thấy anchor '{f.anchor}'")
            continue
        ok = False
        why: str | None = None
        for p, m in hits:
            if any(el is p._p and start == m.start() for el, start in used):
                continue
            if f.type == "table":
                why = _table(doc, p, f)
                ok = why is None
            else:
                ok = _place(doc, p, m, f)
            if ok:
                used.append((p._p, m.start()))
                break
        if ok:
            report.placed.append(f.key)
        else:
            report.missing.append(why or f"{f.key}: không thấy ô trống kiểu '{f.blank}' tại '{f.anchor}'")
    _signatures(doc, schema, report)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), report


def leftover_blanks(docx_bytes: bytes) -> list[str]:
    """Đoạn còn chuỗi dấu chấm dài (dùng cho tự kiểm tra sau khi render)."""
    doc = Document(io.BytesIO(docx_bytes))
    return [ptext(p) for p in iter_paragraphs(doc) if re.search(r"[.…_]{6,}", ptext(p))]
