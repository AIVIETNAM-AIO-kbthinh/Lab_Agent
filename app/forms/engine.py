"""Form engine (TK §7.2–7.6): thu thập → xem trước → xác nhận → lưu qua outbox.

Quy tắc xác nhận: file sẽ lưu khác file user gửi lên ⇒ bắt buộc xem trước + xác nhận.
- agent điền (mode=agent): render → PREVIEWED, nút Xác nhận gắn shown_hash = sha256(bytes đã hiển thị).
- user tự điền, hợp lệ, lưu nguyên file (mode=upload): READY, shown_hash = sha256(file gốc), vẫn hỏi bằng nút.
- Sửa bất kỳ trường nào sau khi xem trước ⇒ bản xem trước cũ mất hiệu lực (nút cũ bị từ chối).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import update

from app import clock
from app.auth.roles import Role, UserContext
from app.db.models import Draft, Upload
from app.domain.dates import fmt_date
from app.domain.names import ascii_key
from app.errors import DraftChanged, DraftInvalid, NotFound, PermissionDenied, ValidationFailed
from app.forms.drafts import CONFIRMABLE, DraftStore
from app.forms.extract import FieldIn, extract_fields, field_in_to_raw
from app.forms.registry import FormDef
from app.forms.render import summary_lines
from app.forms.schema import FieldSpec, FormSchema
from app.forms.validators import (
    DATE_FMT,
    Issue,
    VContext,
    check_draft,
    empty,
    normalize_table,
    normalize_value,
    revalidate_refs,
)
from app.logging import get_logger

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class EngineResult:
    data: dict[str, Any]
    ui_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ConfirmResult:
    ok: bool
    status: str  # SUBMITTED | PROCESSING | NEEDS_EDIT | FAILED
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)


class FormEngine:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.registry = svc.forms
        self.drafts = DraftStore(svc.sm, svc.config.drafts.ttl_hours)

    # ------------------------------------------------------------ dữ liệu sống

    async def vcontext(self, schema: FormSchema, *, fresh: bool = False) -> VContext:
        cfg = self.svc.config
        needs_members = any(
            f.type == "member_ref" or any(c.type == "member_ref" for c in f.columns) or f.default == "leader"
            for f in schema.fields
        )
        needs_equipment = any(
            any(c.type == "equipment_ref" for c in f.columns) or f.type == "equipment_ref"
            for f in schema.fields
        )
        return VContext(
            today=clock.today(),
            allowed_domains=cfg.email.allowed_domains,
            members=await self.svc.members.snapshot(force=fresh) if needs_members else None,
            equipment=await self.svc.equipment.snapshot() if needs_equipment else None,
            advisor_name=cfg.lab.advisor_name,
        )

    async def form(self, form_id: str, ctx: UserContext | None = None) -> FormDef:
        fd = await self.registry.active(form_id)
        if fd is None:
            raise NotFound(f"form {form_id}", user_message=f"Không có loại đơn '{form_id}' đang dùng.")
        if ctx is not None and ctx.role.value not in fd.schema.roles:
            raise PermissionDenied(f"{ctx.role} không được dùng {form_id}")
        return fd

    async def form_of(self, d: Draft) -> FormDef:
        fd = await self.registry.get(d.form_id, d.form_version)
        if fd is None:
            raise DraftInvalid(f"draft {d.id}: form {d.form_id}@{d.form_version} không còn")
        return fd

    # ------------------------------------------------------------ giá trị mặc định / từ tài khoản

    @staticmethod
    def _submitter(ctx: UserContext) -> str:
        """Cột nguoi_nop của FormSubmissions: họ tên trong Members, hoặc "guest" (TK §3.4)."""
        if ctx.ho_ten:
            return ctx.ho_ten
        return "guest" if ctx.role == Role.GUEST else (ctx.email or ctx.role.value)

    def _account_value(self, key: str, ctx: UserContext) -> Any:
        """Giá trị lấy từ tài khoản: dòng Members; admin (không cần trong Members) lấy họ tên/email từ config."""
        labels = self.svc.config.sheets.members.role_labels
        if ctx.role == Role.ADMIN:
            if key == "ho_ten":
                return ctx.ho_ten
            if key == "email":
                return ctx.email or (ctx.member.email if ctx.member else None)
            if key == "role_label":
                return labels.get("admin", "Admin")
        m = ctx.member
        if m is None:
            return None
        if key == "role_label":
            return labels.get(m.role, m.role_raw or m.role)
        return getattr(m, key, None) or None

    async def _defaults(self, schema: FormSchema, ctx: UserContext, vc: VContext) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in schema.fields:
            if f.type in {"attachment", "table"} or f.filled_by == "lab":
                continue
            if f.account_field:
                out[f.key] = self._account_value(f.account_field, ctx)
                continue
            d = f.default
            if d is None:
                continue
            if d == "today":
                out[f.key] = fmt_date(vc.today, DATE_FMT)
            elif d == "leader":
                leader = vc.members.leader() if vc.members else None
                out[f.key] = leader.ho_ten if leader else None
            elif isinstance(d, str) and d.startswith("account."):
                out[f.key] = self._account_value(d.split(".", 1)[1], ctx)
            else:
                n = normalize_value(f, d, vc)
                out[f.key] = d if n.error else n.value
        return {k: v for k, v in out.items() if not empty(v)}

    def _apply_suggestions(
        self, schema: FormSchema, fields: dict[str, Any], meta: dict[str, Any], vc: VContext
    ) -> None:
        """vd vắng mặt = Members trừ người tham dự (đề xuất; người dùng sửa thì không ghi đè)."""
        user_set = set(meta.get("user_set", []))
        for f in schema.fields:
            if not f.suggest or f.key in user_set or vc.members is None:
                continue
            _, _, ref = f.suggest.partition(":")
            present = fields.get(ref)
            if not present:
                continue
            names = set(present if isinstance(present, list) else [present])
            for extra in ("chu_tri", "thu_ky"):
                if fields.get(extra):
                    names.add(fields[extra])
            fields[f.key] = [m.ho_ten for m in vc.members.active if m.ho_ten not in names]
            meta.setdefault("suggested", [])
            if f.key not in meta["suggested"]:
                meta["suggested"].append(f.key)

    # ------------------------------------------------------------ trạng thái gửi LLM

    def _state(
        self,
        d: Draft,
        schema: FormSchema,
        issues: list[Issue],
        *,
        notes: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields = d.fields_json or {}
        missing_msgs = ("Còn thiếu.", "Chưa có file đính kèm.", "Còn trống.")
        missing = [i.label for i in issues if i.message in missing_msgs]
        errors = [i.as_dict() for i in issues if i.message not in missing_msgs]
        filled = {
            f.label: (fields[f.key] if f.type != "table" else f"{len(fields[f.key])} dòng")
            for f in schema.data_fields()
            if not empty(fields.get(f.key))
        }
        for a in schema.attachments():
            if a.key in (d.attachments_json or {}):
                filled[a.label] = d.attachments_json[a.key]["filename"]
        out: dict[str, Any] = {
            "draft_id": d.id,
            "form": schema.title,
            "status": d.status,
            "mode": "user_file" if d.mode == "upload" else "agent_fill",
            "filled": filled,
            "missing": missing,
            "errors": errors,
            "locked_fields": [f.label for f in schema.fields if f.locked],
            # danh sách trường để LLM gửi đúng `key` (kể cả trường không bắt buộc)
            "form_fields": [
                {"key": f.key, "label": f.label, "required": f.required}
                | ({"columns": [c.key for c in f.columns if not c.derived]} if f.type == "table" else {})
                | ({"options": f.options} if f.options else {})
                for f in schema.user_fields()
            ],
        }
        if d.meta_json.get("suggested"):
            specs = [schema.field(k) for k in d.meta_json["suggested"]]
            out["suggested_fields"] = [f.label for f in specs if f is not None]
        if notes:
            out["normalized"] = notes
        if errors:
            out["next_step"] = (
                "Có trường CHƯA được lưu (xem errors). Không nói là đã cập nhật; sửa lại lời gọi nếu sai key, "
                "hoặc hỏi người dùng GỘP các trường còn thiếu/sai trong MỘT tin nhắn."
            )
        elif missing:
            out["next_step"] = (
                "Nếu người dùng ĐÃ nói thông tin cho các trường này trong hội thoại, gọi update_form_fields ngay; "
                "chỉ hỏi GỘP trong MỘT tin nhắn những trường thực sự chưa có."
            )
        elif d.status == "READY":
            out["next_step"] = "Đơn hợp lệ. Nhắc người dùng bấm nút 'Xác nhận nộp' bên dưới."
        else:
            out["next_step"] = "Đủ thông tin. Gọi render_preview để gửi bản xem trước."
        if extra:
            out.update(extra)
        return out

    def _confirm_action(self, d: Draft, schema: FormSchema) -> dict[str, Any]:
        return {
            "type": "form_confirm",
            "draft_id": d.id,
            "shown_hash": d.shown_hash,
            "title": schema.title,
            "filename": (d.meta_json.get("original_filename") or "đơn đã gửi"),
            "summary": summary_lines(schema, d.fields_json, d.attachments_json, d.meta_json),
        }

    async def _after_change(self, d: Draft, schema: FormSchema, issues: list[Issue]) -> list[dict[str, Any]]:
        """Sau mỗi thay đổi: bỏ bản xem trước cũ; file tự điền hợp lệ ⇒ READY + nút xác nhận."""
        d.rendered_bytes = None
        d.rendered_docx = None
        d.preview_mime = None
        d.shown_hash = None
        d.status = "COLLECTING"
        actions: list[dict[str, Any]] = []
        if d.mode == "upload" and not issues and d.original_upload_id:
            up = await self._upload(d.original_upload_id, d.owner_session)
            d.shown_hash = up.sha256
            d.status = "READY"
            actions.append(self._confirm_action(d, schema))
        return actions

    # ------------------------------------------------------------ thao tác (tool gọi)

    async def start(self, form_id: str, ctx: UserContext) -> EngineResult:
        fd = await self.form(form_id, ctx)
        schema = fd.schema
        existing = await self.drafts.find_open(ctx.session_id, form_id)
        vc = await self.vcontext(schema)
        if existing is not None and existing.form_version == fd.version:
            issues = check_draft(schema, existing.fields_json, existing.attachments_json, vc)
            return EngineResult(self._state(existing, schema, issues, extra={"resumed": True}))
        fields = await self._defaults(schema, ctx, vc)
        meta: dict[str, Any] = {"nguoi_nop": self._submitter(ctx)}
        self._apply_suggestions(schema, fields, meta, vc)
        d = await self.drafts.create(
            form_id=form_id,
            form_version=fd.version,
            owner_session=ctx.session_id,
            status="COLLECTING",
            fields_json=fields,
            attachments_json={},
            meta_json=meta,
            mode="agent",
            role=ctx.role.value,
            account_id=ctx.account_id,
            actor=ctx.actor,
        )
        issues = check_draft(schema, fields, {}, vc)
        log.info("form_started", form_id=form_id, draft_id=d.id)
        return EngineResult(self._state(d, schema, issues))

    @staticmethod
    def _field_by_key_or_label(schema: FormSchema, key: str) -> FieldSpec | None:
        """Model yếu hay gửi nhãn ("Họ và tên") thay cho key ("ho_ten"): chấp nhận cả hai."""
        f = schema.field(key.strip())
        if f is not None:
            return f
        k = ascii_key(key)
        return next(
            (x for x in schema.fields if ascii_key(x.label) == k or ascii_key(x.key.replace("_", " ")) == k),
            None,
        )

    def _apply_items(
        self, schema: FormSchema, d: Draft, items: list[FieldIn], vc: VContext
    ) -> tuple[list[Issue], list[str], bool]:
        issues: list[Issue] = []
        notes: list[str] = []
        changed = False
        fields = dict(d.fields_json or {})
        meta = dict(d.meta_json or {})
        unknown: dict[str, list[str]] = dict(meta.get("unknown", {}))
        user_set = set(meta.get("user_set", []))
        for item in items:
            f = self._field_by_key_or_label(schema, item.key)
            if f is None:
                valid = ", ".join(x.key for x in schema.user_fields())
                issues.append(
                    Issue(item.key, item.key, f"Không có trường '{item.key}'. Dùng đúng key: {valid}")
                )
                continue
            if f.type == "attachment":
                issues.append(Issue(f.key, f.label, "File đính kèm phải gắn bằng attach_file với upload_id."))
                continue
            if not f.user_editable:
                why = (
                    "lấy từ thông tin thành viên trong Members"
                    if f.account_field
                    else ("do hệ thống tự điền" if f.filled_by == "system" else "để lab ghi tay")
                )
                issues.append(Issue(f.key, f.label, f"Trường '{f.label}' {why}, không sửa được qua chat."))
                continue
            raw = field_in_to_raw(f, item)
            if empty(raw):
                if f.key in fields:
                    fields.pop(f.key)
                    changed = True
                user_set.add(f.key)
                continue
            if f.type == "table":
                rows, row_issues, row_notes = normalize_table(f, raw, vc)
                issues.extend(row_issues)
                notes.extend(row_notes)
                if not row_issues:
                    fields[f.key] = rows
                    changed = True
                    user_set.add(f.key)
                continue
            n = normalize_value(f, raw, vc)
            if n.error:
                issues.append(Issue(f.key, f.label, n.error, n.options))
                continue
            if fields.get(f.key) != n.value:
                changed = True
            fields[f.key] = n.value
            user_set.add(f.key)
            if n.note:
                notes.append(f"{f.label}: {n.note}")
            if f.type == "member_ref":
                if n.unknown:
                    unknown[f.key] = n.unknown
                else:
                    unknown.pop(f.key, None)
        meta["unknown"] = unknown
        meta["user_set"] = sorted(user_set)
        if "suggested" in meta:
            meta["suggested"] = [k for k in meta["suggested"] if k not in user_set]
        self._apply_suggestions(schema, fields, meta, vc)
        d.fields_json = fields
        d.meta_json = meta
        return issues, notes, changed

    async def update(self, draft_id: str, items: list[FieldIn], ctx: UserContext) -> EngineResult:
        d = await self.drafts.owned(draft_id, ctx)
        fd = await self.form_of(d)
        schema = fd.schema
        vc = await self.vcontext(schema)
        item_issues, notes, changed = self._apply_items(schema, d, items, vc)
        extra: dict[str, Any] = {}
        if changed and d.mode == "upload":
            d.mode = "agent"  # file sẽ lưu khác file user gửi ⇒ phải xem trước + xác nhận
            extra["note"] = "Đơn đã được sửa nên mình sẽ điền lại đơn và gửi bản xem trước để xác nhận."
        all_issues = item_issues + [
            i
            for i in check_draft(schema, d.fields_json, d.attachments_json, vc)
            if i.key not in {x.key for x in item_issues}
        ]
        actions: list[dict[str, Any]] = []
        if changed and d.status == "PREVIEWED":
            extra["preview_invalidated"] = "Bản xem trước cũ không còn hiệu lực; cần gửi bản xem trước mới."
        if changed or d.mode == "upload":
            actions = await self._after_change(d, schema, all_issues)
        await self.drafts.save(d)
        return EngineResult(self._state(d, schema, all_issues, notes=notes, extra=extra), actions)

    async def _upload(self, upload_id: str, session_id: str) -> Upload:
        async with self.svc.sm() as s:
            up = await s.get(Upload, upload_id)
        if up is None or up.session_public_id != session_id or up.expires_at <= clock.now():
            raise ValidationFailed(
                f"upload {upload_id}",
                user_message="Không tìm thấy file đã tải lên (hoặc đã hết hạn), hãy tải lại.",
            )
        return up

    async def attach(self, draft_id: str, key: str, upload_id: str, ctx: UserContext) -> EngineResult:
        d = await self.drafts.owned(draft_id, ctx)
        fd = await self.form_of(d)
        schema = fd.schema
        f = schema.field(key)
        if f is None or f.type != "attachment":
            keys = ", ".join(a.key for a in schema.attachments()) or "(đơn này không có file đính kèm)"
            raise ValidationFailed(user_message=f"'{key}' không phải trường đính kèm. Trường hợp lệ: {keys}")
        up = await self._upload(upload_id, ctx.session_id)
        if f.accept and up.ext not in f.accept:
            raise ValidationFailed(user_message=f"{f.label} chỉ nhận file: {', '.join(f.accept)}.")
        if d.original_upload_id == upload_id:
            raise ValidationFailed(user_message="Đây là file đơn, không phải file đính kèm.")
        att = dict(d.attachments_json or {})
        att[key] = {
            "upload_id": up.id,
            "filename": up.filename,
            "ext": up.ext,
            "sha256": up.sha256,
            "size": up.size,
        }
        d.attachments_json = att
        vc = await self.vcontext(schema)
        issues = check_draft(schema, d.fields_json, att, vc)
        actions = await self._after_change(d, schema, issues)
        await self.drafts.save(d)
        return EngineResult(self._state(d, schema, issues), actions)

    async def submit_filled(self, form_id: str, upload_id: str, ctx: UserContext) -> EngineResult:
        fd = await self.form(form_id, ctx)
        schema = fd.schema
        up = await self._upload(upload_id, ctx.session_id)
        if up.ext not in ("docx", "pdf"):
            raise ValidationFailed(user_message="Đơn tự điền phải là file DOCX hoặc PDF.")
        data = Path(up.path).read_bytes()
        if sha256(data) != up.sha256:
            raise ValidationFailed(
                f"upload {up.id}: hash lệch", user_message="File tải lên bị lỗi, hãy tải lại."
            )
        outcome = await extract_fields(self.svc, schema, data, up.ext, ctx.session_id)
        vc = await self.vcontext(schema)
        fields = await self._defaults(schema, ctx, vc)
        d = Draft(
            form_id=form_id,
            form_version=fd.version,
            owner_session=ctx.session_id,
            status="COLLECTING",
            fields_json=fields,
            attachments_json={},
            meta_json={
                "original_filename": up.filename,
                "extract_method": outcome.method,
                "nguoi_nop": self._submitter(ctx),
            },
            mode="upload",
            role=ctx.role.value,
            account_id=ctx.account_id,
            actor=ctx.actor,
            original_upload_id=up.id,
        )
        items = [self._raw_to_item(schema.field(k), v) for k, v in outcome.raw.items() if schema.field(k)]
        item_issues, notes, _ = self._apply_items(schema, d, [i for i in items if i], vc)
        issues = item_issues + [
            i for i in check_draft(schema, d.fields_json, {}, vc) if i.key not in {x.key for x in item_issues}
        ]
        created = await self.drafts.create(
            **{
                c: getattr(d, c)
                for c in (
                    "form_id",
                    "form_version",
                    "owner_session",
                    "status",
                    "fields_json",
                    "attachments_json",
                    "meta_json",
                    "mode",
                    "role",
                    "account_id",
                    "actor",
                    "original_upload_id",
                )
            }
        )
        actions = await self._after_change(created, schema, issues)
        await self.drafts.save(created)
        extra: dict[str, Any] = {"extracted_from_file": True}
        if outcome.notes:
            extra["note"] = " ".join(outcome.notes)
        if any(f.locked for f in schema.fields):
            extra["account_note"] = (
                "Thông tin người nộp lấy theo tài khoản thành viên, không lấy từ nội dung file."
            )
        if issues:
            extra["how_to_fix"] = (
                "Người dùng có thể gửi lại file đã sửa, hoặc cho biết thông tin đúng qua chat "
                "(khi đó trợ lý điền lại đơn và gửi bản xem trước để xác nhận)."
            )
        return EngineResult(self._state(created, schema, issues, notes=notes, extra=extra), actions)

    @staticmethod
    def _raw_to_item(f: FieldSpec | None, v: Any) -> FieldIn | None:
        if f is None:
            return None
        from app.forms.extract import CellIn, RowIn

        if f.type == "table":
            return FieldIn(
                key=f.key,
                rows=[RowIn(cells=[CellIn(col=k, value=str(x)) for k, x in r.items()]) for r in v],
            )
        if f.multiple and isinstance(v, list):
            return FieldIn(key=f.key, items=[str(x) for x in v])
        return FieldIn(key=f.key, value=str(v))

    async def preview(self, draft_id: str, ctx: UserContext) -> EngineResult:
        d = await self.drafts.owned(draft_id, ctx)
        fd = await self.form_of(d)
        schema = fd.schema
        vc = await self.vcontext(schema)
        issues = check_draft(schema, d.fields_json, d.attachments_json, vc)
        if issues:
            return EngineResult(
                self._state(d, schema, issues, extra={"preview": "Chưa đủ/đúng thông tin để xem trước."})
            )
        if d.mode == "upload" and d.status == "READY":
            return EngineResult(
                self._state(
                    d,
                    schema,
                    [],
                    extra={"note": "Đơn tự điền hợp lệ, lưu nguyên file; không cần bản xem trước."},
                ),
                [self._confirm_action(d, schema)],
            )
        if fd.template_file_id is None:
            raise ValidationFailed(
                f"{fd.form_id}: chưa publish template",
                user_message="Mẫu đơn này chưa sẵn sàng, admin cần chạy publish.",
            )
        fields = dict(d.fields_json)
        for f in schema.fields:  # ngày làm đơn = ngày nộp: làm mới mỗi lần xem trước
            if f.filled_by == "system" and f.default == "today":
                fields[f.key] = fmt_date(vc.today, DATE_FMT)
        template = await self.svc.templates.get(fd.template_file_id)
        res = await self.svc.renderer.render(template, schema, fields, d.attachments_json, d.meta_json)
        d.fields_json = fields
        d.rendered_bytes = res.preview
        d.rendered_docx = res.docx
        d.preview_mime = res.mime
        d.shown_hash = sha256(res.preview)
        d.preview_version = (d.preview_version or 0) + 1
        d.status = "PREVIEWED"
        await self.drafts.save(d)
        summary = summary_lines(schema, fields, d.attachments_json, d.meta_json)
        action = {
            "type": "form_preview",
            "draft_id": d.id,
            "shown_hash": d.shown_hash,
            "version": d.preview_version,
            "title": schema.title,
            "url": f"/drafts/{d.id}/preview?v={d.preview_version}",
            "mime": res.mime,
            "filename": f"{schema.form_id}_v{d.preview_version}.{res.ext}",
            "summary": summary,
        }
        data = self._state(d, schema, [])
        data.update(
            {
                "preview_version": d.preview_version,
                "summary": summary,
                "next_step": "Đã gửi bản xem trước kèm nút Xác nhận/Huỷ. Mời người dùng kiểm tra; muốn sửa gì thì nói, "
                "trợ lý sẽ cập nhật và gửi bản mới. KHÔNG tự xác nhận thay người dùng.",
            }
        )
        return EngineResult(data, [action])

    async def cancel(self, draft_id: str, ctx: UserContext) -> EngineResult:
        d = await self.drafts.owned(draft_id, ctx)
        await self.drafts.set(
            d.id, status="CANCELLED", rendered_bytes=None, rendered_docx=None, shown_hash=None
        )
        return EngineResult({"draft_id": d.id, "status": "CANCELLED", "message": "Đã huỷ bản nháp."})

    # ------------------------------------------------------------ xác nhận (endpoint, KHÔNG phải tool)

    async def shown_bytes(self, d: Draft) -> bytes | None:
        if d.mode == "upload" and d.status in ("READY", "CONFIRMING", "SUBMITTED") and d.original_upload_id:
            up = await self._upload_any(d.original_upload_id)
            return Path(up.path).read_bytes() if up and Path(up.path).exists() else None
        return d.rendered_bytes

    async def _upload_any(self, upload_id: str) -> Upload | None:
        async with self.svc.sm() as s:
            return await s.get(Upload, upload_id)

    async def confirm(self, draft_id: str, shown_hash: str, ctx: UserContext) -> ConfirmResult:
        from app.forms import submit
        from app.outbox.steps import enqueue
        from app.outbox.worker import wait_for

        d = await self.drafts.owned(draft_id, ctx, allow_closed=True)
        if d.status in ("CONFIRMING", "SUBMITTED"):
            return await self._result_of(d, already=True)
        if d.status not in CONFIRMABLE or not d.shown_hash or d.shown_hash != shown_hash:
            if d.status in ("CANCELLED", "FAILED"):
                raise DraftInvalid(f"draft {d.id}: {d.status}")
            raise DraftChanged(f"draft {d.id}: hash {shown_hash[:8]} != {str(d.shown_hash)[:8]} ({d.status})")
        shown = await self.shown_bytes(d)
        if shown is None or sha256(shown) != shown_hash:
            raise DraftChanged(f"draft {d.id}: bytes không khớp shown_hash")
        if not await self.drafts.lock_for_confirm(d.id, ctx.session_id, shown_hash):
            d2 = await self.drafts.load(d.id)
            if d2 is not None and d2.status in ("CONFIRMING", "SUBMITTED"):
                return await self._result_of(d2, already=True)
            raise DraftChanged(f"draft {d.id}: không khoá được (bấm đúp/đã đổi)")

        fd = await self.form_of(d)
        schema = fd.schema
        vc = await self.vcontext(schema, fresh=True)  # recheck_live: số thiết bị còn lại, thành viên
        issues = check_draft(schema, d.fields_json, d.attachments_json, vc) + revalidate_refs(
            schema, d.fields_json, vc
        )
        if issues:
            await self.drafts.set(
                d.id, status="COLLECTING", shown_hash=None, rendered_bytes=None, rendered_docx=None
            )
            return ConfirmResult(
                False,
                "NEEDS_EDIT",
                "Dữ liệu vừa thay đổi nên đơn chưa hợp lệ, vui lòng sửa rồi xác nhận lại.",
                issues=[i.as_dict() for i in issues],
            )

        now = clock.now()
        result: dict[str, Any] = {
            "submission_id": f"{schema.form_id[:3].upper()}-{now:%Y%m%d}-{d.id[:6].upper()}",
            "submitted_at": now.isoformat(),
        }
        result.update(await submit.prepare(self.svc, schema, d, vc))
        keep = timedelta(days=self.svc.config.drafts.keep_after_submit_days)
        upload_ids = [a["upload_id"] for a in (d.attachments_json or {}).values()]
        if d.original_upload_id:
            upload_ids.append(d.original_upload_id)
        async with self.svc.sm() as s, s.begin():
            # mỗi lần xác nhận một op mới (lần trước có thể đã bị từ chối vì dữ liệu sống); bấm đúp đã bị
            # chặn bởi khoá trạng thái CONFIRMING ở trên
            op_id = await enqueue(s, submit.op_kind(schema), {"draft_id": d.id}, actor=d.actor or ctx.actor)
            await s.execute(
                update(Draft)
                .where(Draft.id == d.id)
                .values(op_id=op_id, result_json=result, expires_at=now + keep)
            )
            if upload_ids:
                await s.execute(update(Upload).where(Upload.id.in_(upload_ids)).values(expires_at=now + keep))
        log.info("form_confirmed", form_id=schema.form_id, draft_id=d.id, op_id=op_id)
        await wait_for(self.svc, op_id, self.svc.config.drafts.confirm_wait_s)
        d3 = await self.drafts.load(d.id)
        assert d3 is not None
        return await self._result_of(d3)

    async def _result_of(self, d: Draft, *, already: bool = False) -> ConfirmResult:
        from app.db.models import OutboxOp

        res = dict(d.result_json or {})
        op = None
        if d.op_id:
            async with self.svc.sm() as s:
                op = await s.get(OutboxOp, d.op_id)
        if op is not None and op.status == "FAILED":
            err = op.last_error or ""
            if err.startswith("Precondition:"):
                await self.drafts.set(
                    d.id, status="COLLECTING", shown_hash=None, rendered_bytes=None, rendered_docx=None
                )
                return ConfirmResult(False, "NEEDS_EDIT", err.removeprefix("Precondition:").strip(), res)
            return ConfirmResult(
                False, "FAILED", "Hệ thống chưa lưu được đơn do lỗi kết nối. Admin đã được báo để xử lý.", res
            )
        if d.status == "SUBMITTED" or (op is not None and op.status == "DONE"):
            msg = res.get("message") or "Đã lưu đơn."
            return ConfirmResult(True, "SUBMITTED", ("(Đã gửi trước đó) " if already else "") + msg, res)
        msg = "Đơn đang được xử lý." if already and not res else "Đơn đã được ghi nhận và đang được lưu."
        if res.get("ma_phieu"):
            msg += f" Mã phiếu: {res['ma_phieu']}."
        return ConfirmResult(True, "PROCESSING", msg, res)

    # ------------------------------------------------------------ đơn trống

    async def blank_info(self, form_id: str, ctx: UserContext) -> EngineResult:
        fd = await self.form(form_id, ctx)
        schema = fd.schema
        from app.forms.links import blank_url

        url = blank_url(self.svc, form_id)
        name = Path(schema.template.drive_path).name.removesuffix(".docx") + ".docx"
        intro = schema.intro or "Bạn có thể tải mẫu đơn này về, điền rồi gửi lại cho mình."
        inv = schema.invitation()
        return EngineResult(
            {
                "form_id": form_id,
                "title": schema.title,
                "invitation": f"{intro} 📎 {name}\nHoặc {inv[:1].lower()}{inv[1:]}",
                "note": "Nút tải mẫu đơn đã được gửi kèm câu trả lời. Gửi lời mời trên cho người dùng.",
            },
            [{"type": "download", "url": url, "filename": name, "title": f"Tải mẫu: {schema.title}"}],
        )


def role_allowed(schema: FormSchema, role: Role) -> bool:
    return role.value in schema.roles
