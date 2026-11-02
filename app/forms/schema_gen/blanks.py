"""① Tìm ô trống bằng luật trên docx (TK §7.7).

Các mẫu bắt được:
- `Nhãn: ……` (nhiều ô trên một dòng), `Nhãn: …/…/……` (ngày)
- dòng chỉ có dấu chấm dưới một nhãn/mục đánh số (`next_line`), dòng chấm nối tiếp ô phía trên (nhiều dòng)
- `Nhãn:` không có gì phía sau (`after_anchor`, vd "- CV: ")
- `…, ngày … tháng … năm …` (`date_parts`), "…" giữa câu (`ellipsis`)
- `Nhãn: ☐ A ☐ B` hoặc các dòng ☐ dưới nhãn (`checkbox`)
- bảng có hàng tiêu đề + các hàng trống (`table`), ô bảng có chữ cạnh ô trống bên phải (`cell`)
- ô chữ ký "(Ký và ghi rõ họ tên)" (`signature`)

Mọi `anchor` sinh ra đều là đoạn chữ có thật trong tài liệu (đã kiểm tra lại bằng `anchor_regex`).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from docx import Document
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.domain.names import ascii_key
from app.forms.placeholder import (
    _DOTS_ONLY,
    BOXES,
    HOLE,
    HOLE_RUN,
    TICKED,
    anchor_regex,
    iter_blocks,
    nfc,
    ptext,
)

DATE_HOLES = re.compile(rf"ngày\s*{HOLE}\s*tháng\s*{HOLE}\s*năm\s*{HOLE}", re.IGNORECASE)
DATE_SLASH = re.compile(rf"{HOLE}\s*/\s*{HOLE}\s*/\s*{HOLE}")
INLINE = re.compile(rf"(?P<label>[^:.…_\n{BOXES}]{{1,80}}?)\s*:\s*(?P<hole>{HOLE_RUN})")
ENDS_COLON = re.compile(r"^(?P<label>[^:]{2,80}?)\s*:\s*$")
HEADING = re.compile(r"^\s*(?:[IVXL]+|\d+|[A-ZĐ])[.)]\s+\S")
SIG_MARKERS = ("(ky", "ky va ghi ro", "ky, ghi ro", "ky ten", "ky, ho ten", "ky va ho ten")
# mục do lab/người duyệt ghi: mọi ô bên trong là filled_by=lab
LAB_SECTION = ("xac nhan", "y kien", "danh cho", "do nguoi", "phe duyet", "duyet cua", "ket qua xu ly")
TITLE_WORDS = (
    "don",
    "phieu",
    "giay",
    "bien ban",
    "to khai",
    "ban ",
    "bao cao",
    "ke hoach",
    "de nghi",
    "dang ky",
    "hop dong",
    "quyet dinh",
    "to trinh",
)
NOT_TITLE = ("cong hoa", "doc lap", "phong thi nghiem", "truong", "khoa ", "vien ", "bo ")


@dataclass
class Para:
    idx: int
    p: Paragraph
    text: str  # NFC
    table: int | None = None  # số thứ tự bảng chứa đoạn văn (toàn tài liệu)
    row: int | None = None
    col: int | None = None


@dataclass
class Candidate:
    cid: str
    kind: str  # inline|next_line|after_anchor|date_parts|ellipsis|cell|checkbox|table|signature
    para: int  # đoạn văn chứa anchor
    pos: int  # vị trí ô trống trong đoạn (để sắp đúng thứ tự tài liệu)
    label: str
    anchor: str
    options: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    lab: bool = False  # nằm trong mục lab/người duyệt ghi
    section: str = ""
    multiline: bool = False
    type_hint: str | None = None
    system_date: bool = False  # "Hà Nội, ngày … tháng … năm …" = ngày làm đơn

    def describe(self) -> str:
        extra = ""
        if self.options:
            extra = f" lựa chọn={self.options}"
        if self.columns:
            extra = f" cột={self.columns}"
        flags = (" [mục lab ghi]" if self.lab else "") + (" [nhiều dòng]" if self.multiline else "")
        return (
            f"{self.cid} ({self.kind}, p{self.para}) nhãn='{self.label}' anchor='{self.anchor}'{extra}{flags}"
        )


@dataclass
class Detection:
    paras: list[Para]
    candidates: list[Candidate]
    title: str | None
    warnings: list[str] = field(default_factory=list)

    def fields(self) -> list[Candidate]:
        return [c for c in self.candidates if c.kind != "signature"]

    def signatures(self) -> list[Candidate]:
        return [c for c in self.candidates if c.kind == "signature"]


# ---------------------------------------------------------------- duyệt tài liệu


def walk(doc: DocxDocument) -> list[Para]:
    """Mọi đoạn văn (kể cả trong bảng) theo đúng thứ tự `placeholder.iter_paragraphs`, kèm vị trí trong bảng."""
    out: list[Para] = []
    counter = [0]

    def rec(container: Any, loc: tuple[int | None, int | None, int | None]) -> None:
        for b in iter_blocks(container):
            if isinstance(b, Paragraph):
                out.append(Para(len(out), b, nfc(ptext(b)), *loc))
                continue
            tno = counter[0]
            counter[0] += 1
            seen: list[Any] = []
            for ri, row in enumerate(b.rows):
                for ci, cell in enumerate(row.cells):
                    if any(tc is cell._tc for tc in seen):
                        continue
                    seen.append(cell._tc)
                    rec(cell, (tno, ri, ci))

    rec(doc, (None, None, None))
    return out


def all_tables(doc: DocxDocument) -> list[Table]:
    """Bảng theo thứ tự đánh số của `walk` (bảng lồng được đánh số khi gặp)."""
    out: list[Table] = []

    def rec(container: Any) -> None:
        for b in iter_blocks(container):
            if isinstance(b, Table):
                out.append(b)
                seen: list[Any] = []
                for row in b.rows:
                    for cell in row.cells:
                        if any(tc is cell._tc for tc in seen):
                            continue
                        seen.append(cell._tc)
                        rec(cell)

    rec(doc)
    return out


def canonical(s: str) -> str:
    """Chuỗi anchor: NFC, gộp khoảng trắng, mọi chuỗi chấm → "…"."""
    s = re.sub(HOLE, "…", nfc(s))
    return re.sub(r"\s+", " ", s).strip()


def clean_label(s: str) -> str:
    s = re.sub(HOLE, " ", nfc(s))
    s = re.sub(r"\(.*?\)", lambda m: m.group(0) if len(m.group(0)) < 25 else "", s)
    s = re.sub(r"^\s*[-–•+*]\s*", "", s)
    s = re.sub(r"^\s*(?:[IVXL]+|\d+|[a-zđ])[.)]\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip(" :.;,\t")
    letters = [c for c in s if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        s = s.lower()
    return s[:1].upper() + s[1:] if s else s


def _cell_texts(t: Table) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in t.rows:
        seen: list[Any] = []
        cells: list[str] = []
        for cell in row.cells:
            if any(tc is cell._tc for tc in seen):
                continue
            seen.append(cell._tc)
            cells.append(nfc(cell.text))
        rows.append(cells)
    return rows


def is_signature_text(s: str) -> bool:
    k = ascii_key(s)
    return any(m in k for m in SIG_MARKERS)


def _is_lab_heading(text: str) -> bool:
    k = ascii_key(text)
    return any(w in k for w in LAB_SECTION)


def is_heading(pa: Para) -> bool:
    t = pa.text.strip()
    if not t or len(t) > 140 or pa.table is not None:
        return False
    if HEADING.match(t):
        return True
    letters = [c for c in t if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters)


def find_title(paras: list[Para]) -> str | None:
    caps: list[str] = []
    for pa in paras[:40]:
        t = re.sub(r"\s+", " ", pa.text).strip()
        letters = [c for c in t if c.isalpha()]
        if len(letters) < 4 or not all(c.isupper() for c in letters) or HOLE_RX.search(t):
            continue
        k = ascii_key(t)
        if any(k.startswith(w) for w in NOT_TITLE):
            continue
        if any(k.startswith(w) for w in TITLE_WORDS):
            return clean_label(t)
        caps.append(t)
    return clean_label(caps[0]) if caps else None


HOLE_RX = re.compile(HOLE)
ANY_BOX = BOXES + "☒☑"


# ---------------------------------------------------------------- nhận diện


class _Builder:
    def __init__(self, paras: list[Para]) -> None:
        self.paras = paras
        self.out: list[Candidate] = []
        self.taken: dict[int, list[tuple[int, int]]] = {}  # para → khoảng ký tự đã thuộc một ô
        self.skip: set[int] = set()  # đoạn đã xử lý hết (dòng chấm nối tiếp, dòng ô tick...)
        self.section = ""
        self.lab = False

    def add(self, kind: str, pa: Para, pos: int, label: str, anchor: str, **kw: Any) -> Candidate:
        c = Candidate(
            f"c{len(self.out) + 1}",
            kind,
            pa.idx,
            pos,
            label,
            anchor,
            lab=kw.pop("lab", False) or self.lab,
            section=self.section,
            **kw,
        )
        self.out.append(c)
        return c

    def take(self, idx: int, a: int, b: int) -> None:
        self.taken.setdefault(idx, []).append((a, b))

    def free(self, idx: int, a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in self.taken.get(idx, []))

    def next_nonempty(self, idx: int) -> Para | None:
        for pa in self.paras[idx + 1 :]:
            if pa.text.strip():
                return pa
        return None

    def prev_nonempty(self, idx: int) -> Para | None:
        for pa in reversed(self.paras[:idx]):
            if pa.text.strip():
                return pa
        return None


def detect_doc(doc: DocxDocument) -> Detection:
    paras = walk(doc)
    tables = all_tables(doc)
    b = _Builder(paras)
    warnings: list[str] = []

    # ---- bảng: chữ ký / bảng nhiều dòng / ô nhãn-ô trống
    sig_tables: set[int] = set()
    field_tables: set[int] = set()
    table_cands: list[tuple[int, list[str]]] = []  # (tno, header)
    cell_cands: list[tuple[int, int, int]] = []  # (tno, row, col của ô nhãn)
    for tno, tbl in enumerate(tables):
        rows = _cell_texts(tbl)
        if any(is_signature_text(c) for r in rows for c in r):
            sig_tables.add(tno)
            continue
        header, body = rows[0], rows[1:]
        if (
            body
            and len(header) >= 2
            and all(h.strip() for h in header)
            and all(not c.strip() for r in body for c in r)
        ):
            field_tables.add(tno)
            table_cands.append((tno, [re.sub(r"\s+", " ", h).strip() for h in header]))
            continue
        for ri, r in enumerate(rows):
            for ci in range(len(r) - 1):
                label, right = r[ci].strip(), r[ci + 1].strip()
                if not label or right or HOLE_RX.search(label) or len(label) > 80:
                    continue
                letters = [c for c in label if c.isalpha()]
                if len(label.split()) >= 3 and letters and all(c.isupper() for c in letters):
                    continue  # tiêu đề / quốc hiệu, không phải nhãn
                cell_cands.append((tno, ri, ci))

    # ---- đoạn văn
    label_cells = set(cell_cands)
    lab_at: dict[int, bool] = {}
    for pa in paras:
        t = pa.text
        if is_heading(pa):
            b.section = clean_label(t)
            b.lab = _is_lab_heading(t)
        lab_at[pa.idx] = b.lab
        if not t.strip() or pa.idx in b.skip or pa.table in field_tables:
            continue
        in_sig = pa.table in sig_tables
        is_label_cell = (pa.table, pa.row, pa.col) in label_cells

        # (b) "…, ngày … tháng … năm …"
        for m in DATE_HOLES.finditer(t):
            prefix = t[: m.start()]
            system = bool(re.fullmatch(r"\s*([^\d:,.]{1,30},)?\s*", prefix))
            words = re.findall(r"[^\W\d_]+", prefix)[-2:]
            label = "Ngày làm đơn" if system else clean_label(" ".join([*words, "ngày"]))
            anchor = canonical(t[: m.end()]) if len(prefix) <= 40 else canonical(t[m.start() : m.end()])
            b.add("date_parts", pa, m.start(), label, anchor, type_hint="date", system_date=system)
            b.take(pa.idx, m.start(), m.end())

        # ô tick
        # ô tick (kể cả ô đã tick ☒/☑ trong đơn điền mẫu)
        first_box = next((i for i, ch in enumerate(t) if ch in ANY_BOX), None)
        if first_box is not None and not in_sig:
            head = t[:first_box]
            opts = [clean_label(o) for o in re.split(f"[{ANY_BOX}]", t[first_box + 1 :])]
            opts = [o for o in opts if o]
            if head.strip() and opts:
                label = clean_label(head)
                b.add("checkbox", pa, first_box, label, canonical(head).rstrip(), options=opts)
                b.take(pa.idx, 0, len(t))
                continue

        # `Nhãn: ……` (có thể nhiều ô trên một dòng)
        for m in INLINE.finditer(t):
            if not b.free(pa.idx, m.start("hole"), m.end("hole")):
                continue
            ls = m.start("label") + (len(m["label"]) - len(m["label"].lstrip()))
            colon = t.index(":", m.end("label"))
            anchor = canonical(t[ls : colon + 1])
            label = clean_label(m["label"])
            if not label:
                continue
            hint = "date" if DATE_SLASH.fullmatch(m["hole"].strip()) else None
            c = b.add("inline", pa, m.start("hole"), label, anchor, type_hint=hint)
            b.take(pa.idx, ls, m.end("hole"))
            if not t[m.end("hole") :].strip() and not in_sig:  # ô ở cuối dòng: các dòng chấm sau là phần tiếp
                for q in paras[pa.idx + 1 :]:
                    if not q.text.strip():
                        continue
                    if _DOTS_ONLY.match(q.text) and q.table == pa.table:
                        b.skip.add(q.idx)
                        c.multiline = True
                        continue
                    break

        # `Nhãn:` không có gì phía sau
        em = ENDS_COLON.match(t)
        if em and not HOLE_RX.search(t) and not in_sig and not is_label_cell:
            nxt = b.next_nonempty(pa.idx)
            if nxt is not None and nxt.text.lstrip()[:1] in set(ANY_BOX):
                opts = []
                for q in paras[nxt.idx :]:
                    s = q.text.strip()
                    if not s:
                        continue
                    if s[0] not in ANY_BOX:
                        break
                    opts += [clean_label(o) for o in re.split(f"[{ANY_BOX}]", s) if clean_label(o)]
                    b.skip.add(q.idx)
                b.add("checkbox", pa, len(t), clean_label(em["label"]), canonical(t), options=opts)
                continue
            if nxt is None or not (_DOTS_ONLY.match(nxt.text) and nxt.table == pa.table):
                b.add("after_anchor", pa, len(t), clean_label(em["label"]), canonical(t))
                continue

        # dòng chỉ có dấu chấm → ô nhiều dòng dưới nhãn phía trên
        if _DOTS_ONLY.match(t):
            if in_sig:
                continue
            prev = b.prev_nonempty(pa.idx)
            if prev is None or _DOTS_ONLY.match(prev.text):
                warnings.append(f"Dòng chấm ở đoạn p{pa.idx} không có nhãn phía trên, bỏ qua.")
                continue
            c = b.add("next_line", prev, len(prev.text), clean_label(prev.text), canonical(prev.text))
            c.type_hint = "longtext"
            for q in paras[pa.idx + 1 :]:
                if not q.text.strip():
                    continue
                if _DOTS_ONLY.match(q.text) and q.table == pa.table:
                    b.skip.add(q.idx)
                    c.multiline = True
                    continue
                break
            continue

        # "…" giữa câu
        for m in re.finditer(HOLE_RUN, t):
            if not b.free(pa.idx, m.start(), m.end()):
                continue
            before = t[: m.start()]
            cut = max(before.rfind(ch) for ch in ".;:!?,…")
            for _x, y in b.taken.get(pa.idx, []):
                if y <= m.start():
                    cut = max(cut, y - 1)
            words = re.findall(r"\S+", before[cut + 1 :])[-4:]
            if not words or not any(ch.isalpha() for ch in "".join(words)):
                warnings.append(f"Ô trống không có nhãn đứng trước ở đoạn p{pa.idx}: '{t[:80]}'.")
                continue
            anchor = " ".join(words)
            hint = "date" if DATE_SLASH.fullmatch(m.group(0).strip()) else None
            b.add("ellipsis", pa, m.start(), clean_label(anchor), anchor, type_hint=hint)
            b.take(pa.idx, m.start(), m.end())

    # ---- bảng nhiều dòng: anchor = đoạn văn ngay trước bảng (cùng cấp thân tài liệu)
    body_blocks = list(iter_blocks(doc))
    by_el = {pa.p._p: pa for pa in paras}
    for tno, header in table_cands:
        tbl = tables[tno]
        pos = next(
            (i for i, x in enumerate(body_blocks) if isinstance(x, Table) and x._tbl is tbl._tbl), None
        )
        anchor_pa = None
        if pos is not None:
            for x in reversed(body_blocks[:pos]):
                if isinstance(x, Table):
                    break
                if nfc(ptext(x)).strip():
                    anchor_pa = by_el.get(x._p)
                    break
        if anchor_pa is None:
            warnings.append(f"Bảng {header} không có đoạn văn tiêu đề ngay phía trên, bỏ qua.")
            continue
        b.section = clean_label(anchor_pa.text)
        b.lab = lab_at.get(anchor_pa.idx, False)
        b.add("table", anchor_pa, 10**6, b.section, canonical(anchor_pa.text), columns=header)

    # ---- ô bảng: nhãn bên trái, ô trống bên phải
    for tno, ri, ci in cell_cands:
        label_pa = next(
            (q for q in paras if q.table == tno and q.row == ri and q.col == ci and q.text.strip()), None
        )
        if label_pa is None:
            continue
        b.section = ""
        b.lab = lab_at.get(label_pa.idx, False) or _is_lab_heading(label_pa.text)
        b.add("cell", label_pa, len(label_pa.text), clean_label(label_pa.text), canonical(label_pa.text))

    # ---- chữ ký
    for tno in sorted(sig_tables):
        for r in _cell_texts(tables[tno]):
            for cell_text in r:
                if not is_signature_text(cell_text):
                    continue
                label = re.sub(r"\s+", " ", cell_text.split("(")[0].strip().splitlines()[0]).strip()
                if label:
                    sig_pa = next((q for q in paras if q.table == tno and q.text.strip()), paras[0])
                    b.lab = False
                    b.add("signature", sig_pa, 0, label, label)

    cands = sorted(b.out, key=lambda c: (c.kind == "signature", c.para, c.pos))
    for i, c in enumerate(cands, 1):
        c.cid = f"c{i}"
    for c in cands:
        if c.kind != "signature" and not any(anchor_regex(c.anchor).search(q.text) for q in paras):
            warnings.append(f"anchor '{c.anchor}' không khớp lại tài liệu (bỏ qua).")
    cands = [
        c for c in cands if c.kind == "signature" or any(anchor_regex(c.anchor).search(q.text) for q in paras)
    ]
    return Detection(paras, cands, find_title(paras), warnings)


def detect(data: bytes) -> Detection:
    return detect_doc(Document(io.BytesIO(data)))


def has_ticked_boxes(text: str) -> bool:
    return any(ch in TICKED for ch in text)
