"""Kích hoạt mẫu đơn mới sau khi admin bấm nút (outbox `template_activate`, từng step idempotent).

1. blank    — mẫu trắng → templates/<TenMau>.docx (file cũ của CÙNG form chuyển vào templates/_archive/;
               trùng tên với file khác thì thêm hậu tố). Gắn appProperties để chạy lại không upload trùng.
2. system   — bản có placeholder + schema.yaml → templates/_system/<form_id>/v<version>/.
3. activate — form_versions = active (phiên bản active cũ của form → inactive).
4. audit, finalize.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update

from app.db.models import TemplateDraft
from app.forms.publish import store_system_copy
from app.forms.schema import FormSchema
from app.google.drive import DOCX
from app.logging import get_logger
from app.outbox.steps import OpKind, PreconditionFailed, StepContext, register

log = get_logger(__name__)
KIND = "template_activate"


async def _load(ctx: StepContext) -> tuple[TemplateDraft, FormSchema]:
    async with ctx.svc.sm() as s:
        td = await s.get(TemplateDraft, ctx.payload["template_draft_id"])
        if td is not None:
            s.expunge(td)
    if td is None:
        raise PreconditionFailed("Bản duyệt mẫu đơn không còn tồn tại.")
    raw = dict(td.schema_json)
    raw["version"] = ctx.payload["version"]
    blank = ctx.results.get("blank") or {}
    if blank.get("drive_path"):
        raw["template"] = {"drive_path": blank["drive_path"], "file_id": blank["file_id"]}
    return td, FormSchema.model_validate(raw)


async def _precheck(ctx: StepContext) -> None:
    td, schema = await _load(ctx)
    if td.status not in ("CONFIRMING", "ACTIVATED"):
        raise PreconditionFailed("Bản duyệt mẫu đơn đã bị huỷ hoặc thay đổi.")
    cur = await ctx.svc.forms.active(schema.form_id)
    if cur is not None and cur.schema.hook:
        raise PreconditionFailed(
            f"Mã đơn '{schema.form_id}' đang dùng xử lý đặc thù ({cur.schema.hook}), hãy đổi mã đơn."
        )


async def step_blank(ctx: StepContext) -> dict[str, Any]:
    svc = ctx.svc
    td, schema = await _load(ctx)
    drive = svc.google.drive
    folder_path = svc.config.forms.templates_folder.strip("/")
    folder = await svc.paths.ensure_folder(folder_path)
    children = await drive.list_children(folder.id)
    mine = next((c for c in children if c.app_properties.get("labagent_template_draft") == td.id), None)
    archived: list[str] = []
    if mine is None:
        want = schema.template.drive_path.rsplit("/", 1)[-1]
        base = want.removesuffix(".docx")
        names = {c.name for c in children}
        for c in children:
            if c.is_folder or c.name not in (want, base):
                continue
            if c.app_properties.get("labagent_form") == schema.form_id:  # bản trắng cũ của chính form này
                archive = await svc.paths.ensure_folder(f"{folder_path}/_archive")
                await drive.move(c.id, archive.id, folder.id)
                names.discard(c.name)
                archived.append(c.name)
        name, n = want, 2
        while name in names or name.removesuffix(".docx") in names:
            name, n = f"{base}_{n}.docx", n + 1
        mine = await drive.upload(
            name,
            folder.id,
            td.blank_docx,
            DOCX,
            app_properties={"labagent_template_draft": td.id, "labagent_form": schema.form_id},
        )
        for name_ in archived:
            await svc.paths.forget_path(f"{folder_path}/{name_}")
    return {
        "file_id": mine.id,
        "drive_path": f"{folder_path}/{mine.name}",
        "link": mine.link,
        "archived": archived,
    }


async def step_system(ctx: StepContext) -> dict[str, Any]:
    td, schema = await _load(ctx)
    tpl_file, folder_path, _ = await store_system_copy(ctx.svc, schema, td.template_docx, f"tpl_{td.id}")
    return {"template_file_id": tpl_file.id, "folder": folder_path}


async def step_activate(ctx: StepContext) -> dict[str, Any]:
    _, schema = await _load(ctx)
    await ctx.svc.forms.upsert(
        schema, status="active", template_file_id=ctx.results["system"]["template_file_id"]
    )
    return {"form_id": schema.form_id, "version": schema.version}


async def step_audit(ctx: StepContext) -> None:
    td, schema = await _load(ctx)
    await ctx.svc.audit.log(
        td.actor or ctx.actor,
        ctx.payload.get("role", "admin"),
        "template_activate",
        f"{schema.form_id}@{schema.version}",
        True,
        {"blank": ctx.results["blank"]["link"], "source": td.source_filename},
    )


async def step_finalize(ctx: StepContext) -> dict[str, Any]:
    td, schema = await _load(ctx)
    roles = ", ".join(schema.roles)
    msg = (
        f"Đã kích hoạt mẫu đơn '{schema.title}' (mã {schema.form_id}, phiên bản {schema.version}). "
        f"Người dùng ({roles}) có thể nộp đơn này qua trợ lý ngay bây giờ."
    )
    res = {
        "message": msg,
        "form_id": schema.form_id,
        "version": schema.version,
        "title": schema.title,
        "link": ctx.results["blank"]["link"],
        "drive_path": ctx.results["blank"]["drive_path"],
        "system_folder": ctx.results["system"]["folder"],
    }
    async with ctx.svc.sm() as s, s.begin():
        await s.execute(
            update(TemplateDraft)
            .where(TemplateDraft.id == td.id)
            .values(status="ACTIVATED", result_json=res, shown_hash=None, preview_bytes=None)
        )
    log.info("template_activated", form_id=schema.form_id, version=schema.version)
    return {"message": msg}


register(
    OpKind(
        KIND,
        [
            ("blank", step_blank),
            ("system", step_system),
            ("activate", step_activate),
            ("audit", step_audit),
            ("finalize", step_finalize),
        ],
        precheck=_precheck,
    )
)
