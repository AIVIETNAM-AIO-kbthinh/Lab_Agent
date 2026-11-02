"""Publish schema viết tay (P3): tải đơn trống gốc → chèn placeholder → tự kiểm tra bằng dữ liệu mẫu →
upload templates/_system/<form_id>/v<version>/{template.docx, schema.yaml} → form_versions = active.

P4 (schema_gen) sẽ dùng lại insert_placeholders + self_test, chỉ khác bước sinh schema và bước admin duyệt.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import yaml
from docx import Document

from app.forms.placeholder import insert_placeholders, iter_paragraphs, ptext
from app.forms.render import build_context, render_docx
from app.forms.schema import FormSchema
from app.google.drive import DOCX, GDOC

if TYPE_CHECKING:
    from app.services import Services


SAMPLE_TEXT = {
    "phone": "0912345678",
    "email": "mau@gmail.com",
    "time": "14:00",
    "int": 1,
    "member_ref": "Nguyễn Văn Mẫu",
    "equipment_ref": "Thiết bị mẫu",
}


def sample_values(schema: FormSchema, today: date | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Giá trị mẫu cho mọi trường (tự kiểm tra sau khi chèn placeholder)."""
    today = today or date.today()  # noqa: DTZ011 - chỉ để sinh dữ liệu mẫu
    fields: dict[str, Any] = {}
    atts: dict[str, Any] = {}

    def one(t: str, label: str, options: list[str], multiple: bool) -> Any:
        if t == "date":
            return (today + timedelta(days=1)).strftime("%d/%m/%Y")
        if t == "choice":
            return options[0]
        if t in SAMPLE_TEXT:
            v = SAMPLE_TEXT[t]
            return [v, "Trần Thị Mẫu"] if multiple else v
        if t == "longtext":
            return f"{label} (dòng 1)\n{label} (dòng 2)"
        return f"[{label}]"

    for f in schema.fields:
        if f.filled_by == "lab":
            continue
        if f.type == "attachment":
            atts[f.key] = {"filename": "CV_mau.pdf", "upload_id": "sample", "ext": "pdf"}
        elif f.type == "table":
            fields[f.key] = [
                {
                    c.key: (i if c.type == "row_number" else one(c.type, c.label, c.options, c.multiple))
                    for c in f.columns
                }
                for i in (1, 2)
            ]
        else:
            fields[f.key] = one(f.type, f.label, f.options, f.multiple)
    return fields, atts


def docx_text(data: bytes) -> str:
    return "\n".join(ptext(p) for p in iter_paragraphs(Document(io.BytesIO(data))))


@dataclass
class PublishReport:
    form_id: str
    version: str
    ok: bool
    placed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    template_file_id: str | None = None
    folder: str | None = None
    unchanged: bool = False  # template đã có sẵn (schema + file nguồn không đổi)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def self_test(template: bytes, schema: FormSchema) -> list[str]:
    """Render với dữ liệu mẫu: không còn {{…}}, mọi giá trị mẫu xuất hiện, bảng có đủ dòng."""
    fields, atts = sample_values(schema)
    out = render_docx(template, build_context(schema, fields, atts, {}))
    text = docx_text(out)
    problems = []
    if "{{" in text or "{%" in text:
        problems.append("còn placeholder chưa được thay")
    for f in schema.fields:
        if f.filled_by == "lab" or not f.anchor:
            continue
        v = fields.get(f.key)
        if isinstance(v, str) and f.type not in ("date", "longtext") and v not in text:
            problems.append(f"giá trị mẫu của '{f.key}' không xuất hiện trong bản render")
        if f.type == "table" and str(v[1][f.columns[1].key]) not in text:  # type: ignore[index]
            problems.append(f"bảng '{f.key}' không render đủ dòng")
    return problems


async def load_blank(svc: Services, schema: FormSchema) -> tuple[bytes, str]:
    """(bytes docx của đơn trống, dấu phiên bản file nguồn). Google Doc mỗi lần xuất ra bytes khác nhau nên
    phiên bản lấy theo `version`/`modifiedTime` của Drive chứ không theo bytes."""
    f = await svc.paths.resolve(schema.template.drive_path)
    src = f"{f.id}@{f.version or f.modified_time}"
    if f.mime_type == GDOC:
        return await svc.google.drive.export(f.id, DOCX), src
    if f.mime_type == DOCX:
        return await svc.google.drive.download(f.id), src
    raise ValueError(f"{schema.template.drive_path}: phải là Google Doc hoặc .docx (đang là {f.mime_type})")


def publish_key(schema: FormSchema, source: str) -> str:
    """Không đổi schema + không đổi file nguồn ⇒ cùng key ⇒ publish lại không tạo file mới."""
    body = json.dumps(schema.dump(), ensure_ascii=False, sort_keys=True) + "|" + source
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def publish(
    svc: Services, schema: FormSchema, *, dry_run: bool = False, blank: bytes | None = None
) -> PublishReport:
    rep = PublishReport(schema.form_id, schema.version, ok=False)
    if blank is not None:
        data, source = blank, "bytes:" + hashlib.sha256(blank).hexdigest()
    else:
        data, source = await load_blank(svc, schema)
    template, ph = insert_placeholders(data, schema)
    rep.placed, rep.missing = ph.placed, ph.missing
    if not ph.ok:
        return rep
    rep.problems = self_test(template, schema)
    if rep.problems:
        return rep
    rep.ok = True
    if dry_run:
        return rep
    tpl_file, folder_path, rep.unchanged = await store_system_copy(
        svc, schema, template, publish_key(schema, source)
    )
    await svc.forms.upsert(schema, status="active", template_file_id=tpl_file.id)
    rep.template_file_id = tpl_file.id
    rep.folder = folder_path
    return rep


async def store_system_copy(
    svc: Services, schema: FormSchema, template: bytes, key: str
) -> tuple[Any, str, bool]:
    """Upload templates/_system/<form_id>/v<version>/{template.docx, schema.yaml}; idempotent theo `key`
    (appProperties labagent_key). Trả (file template, đường dẫn folder, đã có sẵn?)."""
    folder_path = f"{svc.config.forms.system_folder.strip('/')}/{schema.form_id}/v{schema.version}"
    folder = await svc.paths.ensure_folder(folder_path)
    children = await svc.google.drive.list_children(folder.id)
    names = {c.name for c in children}
    tpl_file = next(
        (c for c in children if c.name.endswith(".docx") and c.app_properties.get("labagent_key") == key),
        None,
    )
    unchanged = tpl_file is not None
    if tpl_file is None:
        name = "template.docx" if "template.docx" not in names else f"template_{key[:8]}.docx"
        tpl_file = await svc.google.drive.upload(
            name, folder.id, template, DOCX, app_properties={"labagent_key": key}
        )
    if not any(c.name.endswith(".yaml") and c.app_properties.get("labagent_key") == key for c in children):
        body = yaml.safe_dump(schema.dump(), allow_unicode=True, sort_keys=False).encode("utf-8")
        name = "schema.yaml" if "schema.yaml" not in names else f"schema_{key[:8]}.yaml"
        await svc.google.drive.upload(
            name, folder.id, body, "text/yaml", app_properties={"labagent_key": key}
        )
    return tpl_file, folder_path, unchanged
