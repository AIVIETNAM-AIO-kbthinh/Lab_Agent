"""Trích văn bản về dạng markdown (giữ heading): docx (Google Doc export), PDF, HTML web lab."""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from app.domain.dates import parse_sheet_date

_MONTHS_EN = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}

_TEXT_BLOCKS = {"p", "li", "td", "blockquote"}
_BLOCK_CHILD = {"div", "p", "ul", "ol", "li", "table", "section", "h1", "h2", "h3", "h4", "h5", "blockquote"}


@dataclass
class Extracted:
    title: str
    markdown: str
    doc_date: date | None = None
    lang: str | None = None


def clean_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").replace(" ", " ").replace("​", "")
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ------------------------------------------------------------------ docx


def from_docx(data: bytes, title: str = "") -> Extracted:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                lines.append("")
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.match(r"(?i)heading\s*(\d)", style)
            if m:
                lines.append("#" * min(int(m.group(1)), 6) + " " + text)
            elif style.lower() == "title":
                lines.append("# " + text)
            else:
                lines.append(text)
            lines.append("")
        elif tag == "tbl":
            t = Table(child, doc)
            for row in t.rows:
                cells: list[str] = []
                for c in row.cells:
                    ct = " ".join(c.text.split())
                    if not cells or cells[-1] != ct:  # ô merge lặp lại
                        cells.append(ct)
                if any(cells):
                    lines.append(" | ".join(cells))
            lines.append("")
    md = clean_text("\n".join(lines))
    return Extracted(title=title or _first_heading(md) or "", markdown=md, doc_date=minutes_date(md))


# ------------------------------------------------------------------ pdf


def from_pdf(data: bytes, title: str = "") -> Extracted:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:300]:
            parts.append(page.extract_text() or "")
    md = clean_text("\n\n".join(parts))
    return Extracted(title=title, markdown=md, doc_date=minutes_date(md))


def from_plain(data: bytes, title: str = "") -> Extracted:
    md = clean_text(data.decode("utf-8", errors="replace"))
    return Extracted(title=title or _first_heading(md) or "", markdown=md)


def _first_heading(md: str) -> str | None:
    m = re.search(r"^#{1,3}\s+(.+)$", md, re.M)
    return m.group(1).strip() if m else None


_VN_DATE = re.compile(r"ngày\s*(\d{1,2})\s*tháng\s*(\d{1,2})\s*năm\s*(\d{4})", re.I)


def minutes_date(md: str) -> date | None:
    """ "Hà Nội, ngày 12 tháng 10 năm 2026" ở đầu biên bản → ngày họp."""
    m = _VN_DATE.search(md[:2000])
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


# ------------------------------------------------------------------ html


def _date_from_text(s: str) -> date | None:
    s = s.strip()
    d = parse_sheet_date(s) if re.search(r"\d{1,2}/\d{1,2}/\d{4}", s) else None
    if d:
        return d
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS_EN:
        try:
            return date(int(m.group(3)), _MONTHS_EN[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def from_html(
    html: str, *, drop_tags: list[str], drop_class_patterns: list[str], base_title: str = ""
) -> Extracted:
    """Trích nội dung chính theo cấu trúc (h1–h4, p, li, td); trafilatura làm phương án dự phòng."""
    import lxml.html

    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return _trafilatura(html, base_title)
    title_el = root.find(".//title")
    page_title = clean_text(title_el.text_content()) if title_el is not None else base_title

    published = None
    for meta in root.xpath(
        '//meta[@property="article:published_time" or @name="date" or @itemprop="datePublished"]'
    ):
        published = _date_from_text(meta.get("content", "")) or published

    pats = [re.compile(p, re.I) for p in drop_class_patterns]
    for el in list(root.iter()):
        if el.getparent() is None:
            continue
        if not isinstance(el.tag, str):  # comment / processing instruction
            el.drop_tree()
            continue
        cls = el.get("class", "")
        if el.tag.lower() in drop_tags or (cls and any(p.search(c) for p in pats for c in cls.split())):
            el.drop_tree()

    body = root.find(".//body")
    if body is None:
        body = root
    h1 = body.find(".//h1")
    title = " ".join((clean_text(h1.text_content()) if h1 is not None else page_title).split())

    lines: list[str] = []
    seen: set[str] = set()
    for el in body.iter("h1", "h2", "h3", "h4", "h5", "p", "li", "td", "blockquote", "dd", "dt", "div"):
        # bỏ phần tử lồng trong phần tử khối khác đã lấy (vd p trong li)
        if any(a.tag in _TEXT_BLOCKS for a in el.iterancestors()):
            continue
        # div chỉ lấy khi là "lá" chứa chữ trực tiếp (vd tiểu sử trong <div class="...-bio">)
        if el.tag == "div" and (
            not (el.text or "").strip()
            or any(isinstance(c.tag, str) and c.tag in _BLOCK_CHILD for c in el.iterdescendants())
        ):
            continue
        for br in el.iter("br"):
            br.tail = "\n" + (br.tail or "")
        text = clean_text(el.text_content())
        if not text:
            continue
        if el.tag in {"h1", "h2", "h3", "h4", "h5"}:
            line = "#" * int(el.tag[1]) + " " + " ".join(text.split())
        elif el.tag == "li":
            line = "- " + text
        else:
            line = text
        key = line.casefold()
        if key in seen and len(line) < 200:
            continue
        seen.add(key)
        lines.append(line)
    md = clean_text("\n\n".join(lines))

    doc_date = published
    if doc_date is None:
        # trang tin: ngày nằm ngay dưới tiêu đề (".subtitle") hoặc trong vài dòng đầu
        for line in md.split("\n")[:8]:
            doc_date = _date_from_text(line)
            if doc_date:
                break
    lang = root.get("lang")
    if len(md) < 50:
        fb = _trafilatura(lxml.html.tostring(root, encoding="unicode"), title)  # cây đã bỏ nav/footer
        if len(fb.markdown) > len(md):
            fb.doc_date = fb.doc_date or doc_date
            return fb
    return Extracted(title=title, markdown=md, doc_date=doc_date, lang=lang)


def _trafilatura(html: str, title: str) -> Extracted:
    import trafilatura

    md = (
        trafilatura.extract(html, output_format="markdown", include_comments=False, include_tables=True) or ""
    )
    return Extracted(title=title, markdown=clean_text(md))
