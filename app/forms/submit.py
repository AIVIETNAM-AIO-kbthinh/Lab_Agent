"""Lưu đơn sau khi xác nhận (TK §7.6) — chạy trong worker outbox, từng step idempotent.

Xử lý chung: upload file đơn (+ đính kèm) → dòng FormSubmissions/<form_id> → [step của hook] → AuditLog → xong.
- Upload gắn appProperties `labagent_submission` = submission_id: chạy lại không tạo file trùng.
- Dòng FormSubmissions tra theo submission_id trước khi ghi.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db.models import Draft, Upload
from app.domain.dates import fmt_date
from app.forms.opctx import load_draft, naming_vars, safe_name, sheet_value, submitted_at
from app.forms.schema import FormSchema
from app.google.drive import DOCX, GDOC, PDF
from app.hooks.base import HOOKS, Hook
from app.logging import get_logger
from app.outbox.steps import OPS, OpKind, StepContext, StepFn, register

if TYPE_CHECKING:
    from app.forms.validators import VContext
    from app.services import Services

log = get_logger(__name__)
MIME_BY_EXT = {"pdf": PDF, "docx": DOCX}


def op_kind(schema: FormSchema) -> str:
    return f"form_submit:{schema.hook or 'generic'}"


def hook_of(schema: FormSchema) -> Hook | None:
    return HOOKS.get(schema.hook) if schema.hook else None


async def prepare(svc: Services, schema: FormSchema, d: Draft, vc: VContext) -> dict[str, Any]:
    h = hook_of(schema)
    return await h.prepare(svc, schema, d, vc) if h and h.prepare else {}


# ---------------------------------------------------------------- step chung


async def _upload_bytes(svc: Services, upload_id: str) -> tuple[bytes, Upload]:
    async with svc.sm() as s:
        up = await s.get(Upload, upload_id)
    if up is None or not Path(up.path).exists():
        raise FileNotFoundError(f"upload {upload_id} không còn trên đĩa")
    return Path(up.path).read_bytes(), up


async def _main_file(svc: Services, schema: FormSchema, d: Draft) -> tuple[bytes, str, str, str | None]:
    """(bytes, mime, đuôi có dấu chấm, convert_to)."""
    gdoc = schema.output.format == "gdoc"
    if d.mode == "upload" and d.original_upload_id:
        data, up = await _upload_bytes(svc, d.original_upload_id)
        if gdoc and up.ext == "docx":
            return data, DOCX, "", GDOC
        return data, up.mime, f".{up.ext}", None
    if gdoc:
        if not d.rendered_docx:
            raise RuntimeError("Thiếu bản docx đã render")
        return d.rendered_docx, DOCX, "", GDOC
    if not d.rendered_bytes:
        raise RuntimeError("Thiếu bản đã render")
    mime = d.preview_mime or PDF
    return d.rendered_bytes, mime, ".pdf" if mime == PDF else ".docx", None


async def step_upload(ctx: StepContext) -> dict[str, Any]:
    svc = ctx.svc
    d, fd = await load_draft(ctx)
    schema = fd.schema
    sid = (d.result_json or {})["submission_id"]
    v = naming_vars(fd, d)
    folder_path = "/".join(safe_name(p) for p in schema.output.folder.format(**v).split("/") if p)
    folder = await svc.paths.ensure_folder(folder_path)
    children = await svc.google.drive.list_children(folder.id)
    names = {c.name for c in children}

    def existing(role: str) -> Any:
        return next(
            (
                c
                for c in children
                if c.app_properties.get("labagent_submission") == sid
                and c.app_properties.get("labagent_role") == role
            ),
            None,
        )

    def unique(base: str, ext: str) -> str:
        name, n = f"{base}{ext}", 2
        while schema.output.versioning and name in names:
            name, n = f"{base}_v{n}{ext}", n + 1
        names.add(name)
        return name

    main = existing("main")
    if main is None:
        data, mime, ext, convert = await _main_file(svc, schema, d)
        base = safe_name(schema.output.filename.format(**v))
        main = await svc.google.drive.upload(
            unique(base, ext),
            folder.id,
            data,
            mime,
            convert_to=convert,
            app_properties={"labagent_submission": sid, "labagent_role": "main"},
        )
    out: dict[str, Any] = {
        "file_id": main.id,
        "name": main.name,
        "link": main.link,
        "folder_id": folder.id,
        "folder_link": folder.link,
        "folder_path": folder_path,
        "attachments": {},
    }
    for a in schema.attachments():
        info = (d.attachments_json or {}).get(a.key)
        if not info:
            continue
        f = existing(a.key)
        if f is None:
            data, up = await _upload_bytes(svc, info["upload_id"])
            base = safe_name(
                schema.output.attachment_names.get(a.key, f"{v['today']:%Y-%m-%d}_{a.key}").format(**v)
            )
            f = await svc.google.drive.upload(
                unique(base, f".{up.ext}"),
                folder.id,
                data,
                up.mime,
                app_properties={"labagent_submission": sid, "labagent_role": a.key},
            )
        out["attachments"][a.key] = {"file_id": f.id, "name": f.name, "link": f.link}
    return out


async def step_submission_row(ctx: StepContext) -> dict[str, Any]:
    svc = ctx.svc
    d, fd = await load_draft(ctx)
    schema = fd.schema
    h = hook_of(schema)
    res = d.result_json or {}
    up = ctx.results["upload"]
    keys = schema.submission_keys()
    await svc.submissions.ensure_tab(schema.form_id, keys)
    values: dict[str, Any] = {
        "submission_id": res["submission_id"],
        "thoi_gian_nop": submitted_at(d).strftime("%d/%m/%Y %H:%M"),
        "nguoi_nop": (d.meta_json or {}).get("nguoi_nop") or "guest",
        "link_file": up["folder_link"] if h and h.link_folder else up["link"],
        "trang_thai": h.status if h else "DA_NOP",
        "ghi_chu": f"Mã phiếu {res['ma_phieu']}" if res.get("ma_phieu") else "",
    }
    for k in keys:
        values[k] = sheet_value((d.fields_json or {}).get(k))
    row = await svc.submissions.append(schema.form_id, values)
    return {"row": row}


async def step_audit(ctx: StepContext) -> None:
    d, fd = await load_draft(ctx)
    res = d.result_json or {}
    await ctx.svc.audit.log(
        d.actor or ctx.actor,
        d.role,
        f"form_submit:{fd.form_id}",
        res.get("ma_phieu") or res["submission_id"],
        True,
        {"file": ctx.results["upload"]["link"], "submission_id": res["submission_id"]},
    )


async def step_finalize(ctx: StepContext) -> dict[str, Any]:
    from sqlalchemy import update

    d, fd = await load_draft(ctx)
    schema = fd.schema
    h = hook_of(schema)
    up = ctx.results["upload"]
    msg = h.message(schema, d, ctx.results) if h and h.message else f"Đã lưu {schema.title.lower()}."
    res = {**(d.result_json or {}), "message": msg, "file_id": up["file_id"]}
    if d.role != "guest":
        res["link"] = up["link"]
    async with ctx.svc.sm() as s, s.begin():
        await s.execute(update(Draft).where(Draft.id == d.id).values(status="SUBMITTED", result_json=res))
    log.info("form_submitted", form_id=schema.form_id, submission_id=res["submission_id"])
    return {"message": msg}


async def _precheck(ctx: StepContext) -> dict[str, Any] | None:
    d, fd = await load_draft(ctx)
    h = hook_of(fd.schema)
    if h and h.precheck:
        await h.precheck(ctx, fd.schema, d)
    return None


def build_kinds() -> None:
    from app.hooks import application, equipment_borrow, meeting_minutes  # noqa: F401  (đăng ký HOOKS)

    for name in ["generic", *HOOKS]:
        kind = f"form_submit:{name}"
        if kind in OPS:
            continue
        h = HOOKS.get(name)
        steps: list[tuple[str, StepFn]] = [("upload", step_upload), ("submission_row", step_submission_row)]
        steps += list(h.steps) if h else []
        steps += [("audit", step_audit), ("finalize", step_finalize)]
        register(OpKind(kind, steps, precheck=_precheck))


build_kinds()

__all__ = ["build_kinds", "fmt_date", "op_kind", "prepare"]
