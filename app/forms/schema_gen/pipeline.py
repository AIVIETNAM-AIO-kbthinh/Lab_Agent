"""Pipeline sinh schema (TK §7.7): ① luật → ② LLM rà soát → tẩy trắng → ③ kiểu dữ liệu → ④ chèn placeholder
→ ⑤ tự kiểm tra bằng dữ liệu mẫu. Không ghi Drive/DB: kết quả được admin duyệt rồi mới lưu (review.py).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from docx import Document

from app.errors import LabAgentError
from app.forms.placeholder import (
    _DOTS_ONLY,
    anchor_regex,
    insert_placeholders,
    set_paragraph_text,
)
from app.forms.publish import self_test
from app.forms.schema import FormSchema
from app.forms.schema_gen.blanks import all_tables, detect, is_signature_text, walk
from app.forms.schema_gen.build import BuildNotes, build_schema, schema_without
from app.forms.schema_gen.llm_sweep import FormAnalysis, analyze
from app.forms.schema_gen.whiteout import whiteout
from app.logging import get_logger

if TYPE_CHECKING:
    from app.llm.client import LLMClient

log = get_logger(__name__)


class GenerationFailed(LabAgentError):
    user_message = "Không sinh được schema cho mẫu đơn này."

    def __init__(self, reason: str) -> None:
        super().__init__(reason, user_message=reason)


@dataclass
class GenReport:
    method: str = "rules"  # rules | rules+llm
    filled_sample: bool = False
    wiped: list[str] = field(default_factory=list)
    wipe_not_found: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class GenResult:
    schema: FormSchema
    blank: bytes  # mẫu trắng (đã tẩy thông tin mẫu) — file member tải về
    template: bytes  # bản sao có placeholder docxtpl
    report: GenReport


def check_docx(data: bytes) -> None:
    try:
        Document(io.BytesIO(data))
    except Exception as e:
        raise GenerationFailed("File không phải tài liệu Word (.docx) hợp lệ.") from e


def clear_continuations(template: bytes) -> bytes:
    """Bỏ các dòng chấm nối tiếp ngay dưới một ô đã chèn placeholder (ô nhiều dòng chỉ còn một placeholder)."""
    doc = Document(io.BytesIO(template))
    prev_has_ph = False
    changed = False
    for pa in walk(doc):
        t = pa.text
        if not t.strip():
            continue
        if _DOTS_ONLY.match(t) and prev_has_ph:
            set_paragraph_text(pa.p, "")
            changed = True
            continue
        prev_has_ph = "{{" in t and "{%" not in t
    if not changed:
        return template
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def build_template(blank: bytes, schema: FormSchema) -> tuple[bytes, FormSchema, list[str]]:
    """Chèn placeholder; trường không đặt được thì bỏ (kèm lý do) rồi chèn lại trên bản trắng."""
    dropped: list[str] = []
    for _ in range(3):
        tpl, rep = insert_placeholders(blank, schema)
        if rep.ok:
            return clear_continuations(tpl), schema, dropped
        need = {f.key for f in schema.fields if f.anchor and f.filled_by != "lab"}
        bad = need - set(rep.placed)
        unsign = {s.label for s in schema.signatures if s.field and s.label not in rep.signatures}
        dropped += rep.missing
        schema = schema_without(schema, bad, unsign)
    raise GenerationFailed("Không chèn được vị trí điền cho mẫu đơn: " + "; ".join(dropped[:5]))


def leftover_lines(template: bytes, schema: FormSchema) -> list[str]:
    """Dòng còn chuỗi chấm dài trong mẫu (ô có thể bị bỏ sót), trừ phần lab ghi tay và ô ký."""
    doc = Document(io.BytesIO(template))
    sig = {
        i
        for i, t in enumerate(all_tables(doc))
        if any(is_signature_text(c.text) for r in t.rows for c in r.cells)
    }
    lab = [anchor_regex(f.anchor) for f in schema.fields if f.filled_by == "lab" and f.anchor]
    out: list[str] = []
    prev = ""
    for pa in walk(doc):
        t = pa.text
        if not t.strip():
            continue
        if re.search(r"[.…_]{6,}", t) and pa.table not in sig:
            excused = any(rx.search(t) or (_DOTS_ONLY.match(t) and rx.search(prev)) for rx in lab)
            if not excused:
                out.append(t.strip()[:120])
        if not _DOTS_ONLY.match(t):
            prev = t
    return out


async def generate(
    data: bytes,
    filename: str,
    *,
    llm: LLMClient | None,
    today: date,
    roles: list[str] | None = None,
    title: str | None = None,
    form_id: str | None = None,
    admin_note: str | None = None,
    templates_folder: str = "templates",
    submissions_folder: str = "submissions",
    default_roles: list[str] | None = None,
    session_id: str | None = None,
    account_id: str | None = None,
) -> GenResult:
    check_docx(data)
    rep = GenReport()
    det0 = detect(data)
    analysis: FormAnalysis | None = None
    if llm is not None:
        try:
            analysis = await analyze(
                llm, det0, filename, admin_note=admin_note, session_id=session_id, account_id=account_id
            )
            rep.method = "rules+llm"
        except Exception as e:  # LLM lỗi: vẫn sinh theo luật, admin kiểm tra kỹ hơn
            log.warning("schema_gen_llm_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            rep.warnings.append("Không dùng được AI để rà soát; schema chỉ dựa trên luật, hãy kiểm tra kỹ.")

    blank, det = data, det0
    if analysis is not None and (analysis.is_filled_sample or analysis.sample_values):
        blank, wrep = whiteout(data, analysis.sample_values, filled=True)
        rep.filled_sample = True
        rep.wiped, rep.wipe_not_found = wrep.wiped, wrep.not_found
        det = detect(blank)
    rep.warnings += det.warnings
    if not det.fields():
        if analysis is None and not det0.fields():
            raise GenerationFailed(
                "Không tìm thấy ô trống nào trong đơn. Nếu đây là đơn đã điền thông tin mẫu, hãy thử lại sau "
                "(cần AI để tẩy trắng), hoặc gửi bản đơn trắng."
            )
        raise GenerationFailed("Không tìm thấy ô cần điền nào trong đơn.")
    schema, notes = build_schema(
        det,
        analysis,
        orig=det0,
        filename=filename,
        roles=roles,
        title=title,
        form_id=form_id,
        today=today,
        templates_folder=templates_folder,
        submissions_folder=submissions_folder,
        default_roles=default_roles,
    )
    rep.warnings += notes.warnings
    rep.notes = notes.llm_notes
    template, schema, dropped = build_template(blank, schema)
    rep.dropped = dropped
    problems = self_test(template, schema)
    if problems:
        raise GenerationFailed("Tự kiểm tra bằng dữ liệu mẫu không đạt: " + "; ".join(problems[:5]))
    rep.leftovers = leftover_lines(template, schema)
    log.info(
        "schema_generated",
        form_id=schema.form_id,
        fields=len(schema.fields),
        method=rep.method,
        filled_sample=rep.filled_sample,
    )
    return GenResult(schema, blank, template, rep)


__all__ = ["BuildNotes", "GenResult", "GenerationFailed", "build_template", "generate", "leftover_lines"]
