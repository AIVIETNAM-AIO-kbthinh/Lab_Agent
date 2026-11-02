"""Admin duyệt mẫu đơn mới qua chat (P4).

1. Admin gửi file .docx (đơn trắng hoặc đơn đã điền mẫu) → tool `propose_template`: tẩy trắng, sinh schema, điền
   thử dữ liệu mẫu → thẻ xem trước + nút Kích hoạt/Huỷ (giống thẻ xác nhận đơn của P3).
2. Admin yêu cầu sửa bằng lời → tool `update_template_draft` → bản xem trước mới; nút của bản cũ bị từ chối
   (shown_hash đổi).
3. Bấm Kích hoạt (endpoint, KHÔNG phải tool) → outbox `template_activate`: lưu mẫu trắng vào templates/, bản có
   placeholder + schema.yaml vào templates/_system/<form_id>/v<version>/, form_versions = active.
   Từ đó member dùng được mẫu mới qua form engine (xử lý chung: lưu file + ghi FormSubmissions).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update

from app import clock
from app.auth.roles import Role, UserContext
from app.db.models import FormVersion, OutboxOp, TemplateDraft, Upload
from app.domain.names import ascii_key
from app.errors import DraftChanged, DraftInvalid, PermissionDenied, ValidationFailed
from app.forms.engine import EngineResult
from app.forms.placeholder import anchor_regex
from app.forms.publish import sample_values, self_test
from app.forms.schema import FormSchema
from app.forms.schema_gen.blanks import walk
from app.forms.schema_gen.build import (
    ROLES_ORDER,
    apply_roles,
    file_base,
    output_filename,
    sanitize_key,
    slug,
)
from app.forms.schema_gen.pipeline import GenerationFailed, build_template, generate, leftover_lines
from app.forms.schema_gen.types import infer_field
from app.logging import get_logger

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)

ROLE_VI = {"guest": "Khách (người ngoài lab)", "member": "Thành viên", "thu_ki": "Thư ký", "leader": "Leader"}
TYPE_VI = {
    "text": "Văn bản",
    "longtext": "Văn bản dài",
    "phone": "Số điện thoại",
    "email": "Email",
    "date": "Ngày",
    "time": "Giờ",
    "int": "Số",
    "choice": "Lựa chọn",
    "member_ref": "Tên thành viên lab",
    "equipment_ref": "Thiết bị lab",
    "table": "Bảng",
    "attachment": "File đính kèm",
    "row_number": "Số thứ tự",
}
FieldTypeIn = Literal[
    "text", "longtext", "phone", "email", "date", "time", "int", "choice", "member_ref", "attachment"
]
BlankIn = Literal["inline", "next_line", "after_anchor", "ellipsis", "cell", "checkbox"]
RoleIn = Literal["guest", "member", "thu_ki", "leader"]
OPEN = "PREVIEWED"


class FieldChange(BaseModel):
    action: Literal["update", "remove", "add"] = Field(
        description="update: sửa trường/cột; remove: bỏ trường; add: thêm trường tại một nhãn có trong đơn."
    )
    key: str = Field(description="key (hoặc nhãn) của trường cần sửa/bỏ; với add: key mới (snake_case).")
    column: str | None = Field(None, description="Chỉ khi sửa một CỘT của trường bảng: key cột.")
    label: str | None = Field(None, description="Nhãn mới.", max_length=200)
    type: FieldTypeIn | None = Field(None, description="Kiểu dữ liệu mới.")
    required: bool | None = None
    options: list[str] | None = Field(None, description="Các lựa chọn (kiểu choice).", max_length=30)
    multiple: bool | None = Field(None, description="Nhiều giá trị (tên thành viên).")
    filled_by: Literal["user", "lab"] | None = Field(
        None, description="user: người nộp điền; lab: để trống cho lab/người duyệt ghi tay."
    )
    hint: str | None = Field(None, description="Gợi ý cho trợ lý khi hỏi trường này.", max_length=300)
    new_key: str | None = Field(None, description="Đổi key trường.", max_length=40)
    anchor: str | None = Field(
        None,
        description="Chỉ cho add: đoạn chữ NGUYÊN VĂN trong đơn đứng ngay trước ô trống.",
        max_length=200,
    )
    blank: BlankIn | None = Field(None, description="Chỉ cho add: vị trí ô so với anchor (mặc định inline).")


@dataclass
class ReviewResult:
    ok: bool
    status: str  # ACTIVATED | PROCESSING | NEEDS_EDIT | FAILED
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def content_hash(preview: bytes, schema: dict[str, Any]) -> str:
    """Nút Kích hoạt gắn với ĐÚNG bản xem trước + schema admin đã thấy."""
    body = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(preview + b"|" + body).hexdigest()


# ---------------------------------------------------------------- mô tả cho admin


def _field_desc(f: dict[str, Any]) -> str:
    if f.get("filled_by") == "lab":
        return "Để trống — lab/người duyệt ghi tay"
    if f.get("filled_by") == "system":
        return "Hệ thống tự điền (ngày nộp)"
    if f.get("locked"):
        return "Lấy từ tài khoản thành viên (không sửa được)"
    t = f["type"]
    parts = [TYPE_VI.get(t, t)]
    if t == "choice":
        parts = ["Lựa chọn: " + " / ".join(f.get("options", []))]
    if t == "table":
        parts = ["Bảng: " + ", ".join(c["label"] for c in f.get("columns", []))]
    if f.get("multiple"):
        parts.append("nhiều giá trị")
    parts.append("bắt buộc" if f.get("required") else "không bắt buộc")
    return " · ".join(parts)


def summary_rows(schema: dict[str, Any]) -> list[list[str]]:
    roles = ", ".join(ROLE_VI.get(r, r) for r in schema["roles"])
    folder = schema["output"]["folder"].replace("{today:%Y-%m}", "<năm-tháng>")
    rows = [
        ["Tên đơn", schema["title"]],
        ["Mã đơn (form_id)", schema["form_id"]],
        ["Ai dùng được", roles],
        ["Lưu đơn nộp vào", folder + "/"],
        ["Mẫu trắng lưu tại", schema["template"]["drive_path"]],
    ]
    for f in schema["fields"]:
        rows.append([f"• {f['label']}", _field_desc(f)])
    sigs = [
        s["label"] + (" (in họ tên người nộp)" if s.get("field") else " (ký tay)")
        for s in schema["signatures"]
    ]
    if sigs:
        rows.append(["Chữ ký", "; ".join(sigs)])
    return rows


def _llm_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for f in schema["fields"]:
        d: dict[str, Any] = {
            "key": f["key"],
            "label": f["label"],
            "type": f["type"],
            "required": f["required"],
            "filled_by": f["filled_by"],
        }
        if f.get("locked"):
            d["from_account"] = True
        if f.get("options"):
            d["options"] = f["options"]
        if f["type"] == "table":
            d["columns"] = [
                {"key": c["key"], "label": c["label"], "type": c["type"]} for c in f.get("columns", [])
            ]
        out.append(d)
    return out


# ---------------------------------------------------------------- dịch vụ


class TemplateReview:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.cfg = svc.config.forms

    @staticmethod
    def _require_admin(ctx: UserContext) -> None:
        if ctx.role not in (Role.LEADER, Role.ADMIN):
            raise PermissionDenied(f"{ctx.role} không được thêm mẫu đơn")

    async def _upload(self, upload_id: str, ctx: UserContext) -> tuple[bytes, Upload]:
        async with self.svc.sm() as s:
            up = await s.get(Upload, upload_id)
        if up is None or up.session_public_id != ctx.session_id or up.expires_at <= clock.now():
            raise ValidationFailed(
                f"upload {upload_id}",
                user_message="Không tìm thấy file đã tải lên (hoặc đã hết hạn), hãy tải lại.",
            )
        if up.ext != "docx":
            raise ValidationFailed(
                user_message="Mẫu đơn cần là file Word (.docx). PDF hoặc ảnh scan không chèn được ô điền; "
                "hãy gửi bản Word (có thể xuất từ Google Docs: Tệp → Tải xuống → .docx)."
            )
        data = Path(up.path).read_bytes()
        if hashlib.sha256(data).hexdigest() != up.sha256:
            raise ValidationFailed(
                f"upload {up.id}: hash lệch", user_message="File tải lên bị lỗi, hãy tải lại."
            )
        return data, up

    async def owned(self, td_id: str, ctx: UserContext, *, allow_closed: bool = False) -> TemplateDraft:
        self._require_admin(ctx)
        async with self.svc.sm() as s:
            td = await s.get(TemplateDraft, td_id)
            if td is not None:
                s.expunge(td)
        if td is None or td.owner_session != ctx.session_id:
            raise DraftInvalid(
                f"template draft {td_id}: không tồn tại/không thuộc session",
                user_message="Không tìm thấy bản duyệt mẫu đơn này.",
            )
        if not allow_closed and (td.status != OPEN or td.expires_at <= clock.now()):
            raise DraftInvalid(
                f"template draft {td_id}: {td.status}",
                user_message="Bản duyệt mẫu đơn này không còn mở (đã kích hoạt, huỷ hoặc hết hạn).",
            )
        return td

    async def _conflicts(self, schema: FormSchema) -> tuple[list[str], str | None]:
        """(cảnh báo, lý do chặn kích hoạt)."""
        cur = await self.svc.forms.active(schema.form_id)
        if cur is None:
            return [], None
        if cur.schema.hook:
            return [], (
                f"Mã đơn '{schema.form_id}' đang là '{cur.schema.title}' có xử lý đặc thù ({cur.schema.hook}); "
                "không thay bằng mẫu tự sinh được. Nếu đây là đơn khác, đổi mã đơn (form_id); nếu muốn thay mẫu "
                "đơn này, cần người phát triển cập nhật schema."
            )
        return [
            f"Đã có mẫu '{cur.schema.title}' (mã {schema.form_id}, phiên bản {cur.version}): kích hoạt sẽ thay "
            "phiên bản đang dùng; đơn đang điền dở vẫn dùng bản cũ."
        ], None

    async def _render(self, template: bytes, schema: FormSchema) -> tuple[bytes, str]:
        fields, atts = sample_values(schema, clock.today())
        res = await self.svc.renderer.render(template, schema, fields, atts, {})
        return res.preview, res.mime

    def _ui(self, td: TemplateDraft, report: dict[str, Any]) -> dict[str, Any]:
        warnings = list(report.get("warnings", []))
        if report.get("leftovers"):
            warnings.append(
                "Còn chỗ trống chưa nhận diện: " + " | ".join(f"“{x[:50]}”" for x in report["leftovers"][:5])
            )
        warnings += report.get("conflicts", [])
        return {
            "type": "template_preview",
            "template_draft_id": td.id,
            "shown_hash": td.shown_hash,
            "version": td.preview_version,
            "title": td.schema_json["title"],
            "url": f"/admin/api/templates/{td.id}/preview?v={td.preview_version}",
            "mime": td.preview_mime,
            "blank_url": f"/admin/api/templates/{td.id}/blank?v={td.preview_version}",
            "filename": f"{td.form_id}_dien_thu_v{td.preview_version}",
            "summary": summary_rows(td.schema_json),
            "warnings": warnings,
            "blocked": report.get("blocked"),
            "wiped": len(report.get("wiped", [])),
        }

    def _state(
        self, td: TemplateDraft, report: dict[str, Any], extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        from app.tools.registry import untrusted

        sch = td.schema_json
        data: dict[str, Any] = {
            "template_draft_id": td.id,
            "status": td.status,
            "preview_version": td.preview_version,
            "form_id": sch["form_id"],
            "roles": sch["roles"],
            "content": untrusted(
                {
                    "title": sch["title"],
                    "description": sch["description"],
                    "fields": _llm_fields(sch),
                    "signatures": sch["signatures"],
                    "warnings": report.get("warnings", []),
                    "not_recognized_blanks": report.get("leftovers", []),
                    "notes": report.get("notes", []),
                }
            ),
            "filled_sample_wiped": report.get("filled_sample", False),
            "method": report.get("method"),
            "next_step": (
                "Đã hiện thẻ xem trước (bản điền thử + mẫu trắng) kèm nút Kích hoạt/Huỷ. Tóm tắt ngắn: số trường, "
                "các lưu ý; hỏi admin GỘP trong một tin nhắn: (1) ai được dùng đơn (hiện tại: roles), (2) có cần "
                "thêm/sửa/bỏ trường nào không. Admin muốn sửa → update_template_draft. KHÔNG tự kích hoạt, KHÔNG "
                "nói là đã lưu."
            ),
        }
        if report.get("blocked"):
            data["blocked"] = report["blocked"]
            data["next_step"] = (
                "Chưa kích hoạt được: "
                + report["blocked"]
                + " Hỏi admin mã đơn mới rồi gọi update_template_draft."
            )
        if report.get("filled_sample"):
            data["wiped_count"] = len(report.get("wiped", []))
        if extra:
            data.update(extra)
        return data

    # ------------------------------------------------------------ tool: đề xuất

    async def propose(
        self,
        upload_id: str,
        ctx: UserContext,
        *,
        roles: list[str] | None = None,
        title: str | None = None,
        form_id: str | None = None,
        note: str | None = None,
    ) -> EngineResult:
        self._require_admin(ctx)
        data, up = await self._upload(upload_id, ctx)
        gen = await generate(
            data,
            up.filename,
            llm=self.svc.llm,
            today=clock.today(),
            roles=roles or None,
            title=title,
            form_id=form_id,
            admin_note=note,
            templates_folder=self.cfg.templates_folder,
            submissions_folder=self.cfg.submissions_folder,
            default_roles=self.cfg.default_roles,
            session_id=ctx.session_id,
            account_id=ctx.account_id,
        )
        schema = gen.schema
        conflicts, blocked = await self._conflicts(schema)
        preview, mime = await self._render(gen.template, schema)
        report = gen.report.as_dict() | {"conflicts": conflicts, "blocked": blocked}
        dumped = schema.dump()
        td = TemplateDraft(
            owner_session=ctx.session_id,
            account_id=ctx.account_id,
            actor=ctx.actor,
            status=OPEN,
            form_id=schema.form_id,
            schema_json=dumped,
            source_filename=up.filename,
            source_sha256=up.sha256,
            blank_docx=gen.blank,
            template_docx=gen.template,
            preview_bytes=preview,
            preview_mime=mime,
            preview_version=1,
            shown_hash=content_hash(preview, dumped),
            report_json=report,
            expires_at=clock.now() + timedelta(hours=self.cfg.review_ttl_hours),
        )
        async with self.svc.sm() as s, s.begin():
            s.add(td)
            await s.flush()
            s.expunge(td)
        log.info(
            "template_proposed", template_draft_id=td.id, form_id=schema.form_id, fields=len(schema.fields)
        )
        return EngineResult(self._state(td, report), [self._ui(td, report)])

    # ------------------------------------------------------------ tool: sửa

    async def update(
        self,
        td_id: str,
        ctx: UserContext,
        *,
        changes: list[FieldChange] | None = None,
        title: str | None = None,
        description: str | None = None,
        roles: list[str] | None = None,
        form_id: str | None = None,
        intro: str | None = None,
    ) -> EngineResult:
        td = await self.owned(td_id, ctx)
        raw = json.loads(json.dumps(td.schema_json))
        errors: list[str] = []
        notes: list[str] = []
        if title and title.strip():
            raw["title"] = title.strip()[:200]
            base = file_base(raw["title"])
            raw["template"]["drive_path"] = f"{self.cfg.templates_folder.strip('/')}/{base}.docx"
        if description and description.strip():
            raw["description"] = description.strip()[:500]
        if intro is not None:
            raw["intro"] = intro.strip()[:300] or None
        if form_id and form_id.strip():
            fid = slug(form_id)
            raw["form_id"] = fid
            raw["output"]["folder"] = f"{self.cfg.submissions_folder.strip('/')}/{fid}/{{today:%Y-%m}}"
        if roles:
            raw["roles"] = [r for r in ROLES_ORDER if r in set(roles)]
            apply_roles(raw["fields"], raw["roles"])
        for ch in changes or []:
            try:
                notes += self._apply_change(raw, ch, td.blank_docx)
            except ValueError as e:
                errors.append(str(e))
        raw["output"]["filename"] = output_filename(raw)
        try:
            schema = FormSchema.model_validate(raw)
        except ValidationError as e:
            msgs = "; ".join(er["msg"] for er in e.errors()[:5])
            raise ValidationFailed(user_message=f"Thay đổi không hợp lệ: {msgs}") from e
        try:
            template, schema, dropped = build_template(td.blank_docx, schema)
        except GenerationFailed as e:
            raise ValidationFailed(user_message=e.user_message) from e
        problems = self_test(template, schema)
        if problems:
            raise ValidationFailed(user_message="Tự kiểm tra không đạt: " + "; ".join(problems[:5]))
        report = dict(td.report_json or {})
        if dropped:
            errors += [f"Không đặt được vào đơn nên đã bỏ: {d}" for d in dropped]
        report["leftovers"] = leftover_lines(template, schema)
        report["conflicts"], report["blocked"] = await self._conflicts(schema)
        preview, mime = await self._render(template, schema)
        dumped = schema.dump()
        new_hash = content_hash(preview, dumped)
        async with self.svc.sm() as s, s.begin():
            res = await s.execute(
                update(TemplateDraft)
                .where(TemplateDraft.id == td.id, TemplateDraft.status == OPEN)
                .values(
                    schema_json=dumped,
                    form_id=schema.form_id,
                    template_docx=template,
                    preview_bytes=preview,
                    preview_mime=mime,
                    preview_version=td.preview_version + 1,
                    shown_hash=new_hash,
                    report_json=report,
                )
                .returning(TemplateDraft.id)
            )
            if res.scalar_one_or_none() is None:
                raise DraftInvalid(user_message="Bản duyệt mẫu đơn này không còn mở.")
        td = await self.owned(td.id, ctx, allow_closed=True)
        extra: dict[str, Any] = {"preview_invalidated": "Thẻ xem trước cũ không còn hiệu lực."}
        if errors:
            extra["errors"] = errors
        if notes:
            extra["applied"] = notes
        return EngineResult(self._state(td, report, extra), [self._ui(td, report)])

    @staticmethod
    def _find(raw: dict[str, Any], key: str) -> dict[str, Any] | None:
        k = key.strip()
        f = next((x for x in raw["fields"] if x["key"] == k), None)
        if f is None:
            ak = ascii_key(k)
            f = next((x for x in raw["fields"] if ascii_key(x["label"]) == ak), None)
        return f

    def _apply_change(self, raw: dict[str, Any], ch: FieldChange, blank: bytes) -> list[str]:
        if ch.action == "remove":
            f = self._find(raw, ch.key)
            if f is None:
                raise ValueError(f"Không có trường '{ch.key}'.")
            raw["fields"] = [x for x in raw["fields"] if x is not f]
            for s in raw["signatures"]:
                if s.get("field") == f["key"]:
                    s["field"] = None
            for x in raw["fields"]:
                for rel in ("not_before", "not_after", "after", "before"):
                    if x.get(rel) == f["key"]:
                        x[rel] = None
                if x.get("suggest") and x["suggest"].endswith(":" + f["key"]):
                    x["suggest"] = None
            return [f"Đã bỏ trường '{f['label']}'."]
        if ch.action == "add":
            return self._add(raw, ch, blank)
        f = self._find(raw, ch.key)
        if f is None:
            raise ValueError(
                f"Không có trường '{ch.key}'. Key hợp lệ: {', '.join(x['key'] for x in raw['fields'])}"
            )
        if ch.column:
            col = next((c for c in f.get("columns", []) if c["key"] == ch.column), None)
            if col is None:
                raise ValueError(f"Trường '{f['label']}' không có cột '{ch.column}'.")
            if ch.label and ch.label != col["label"]:
                raise ValueError("Không đổi được tiêu đề cột (phải giống tiêu đề in trong đơn).")
            if ch.type:
                if ch.type in ("attachment", "choice"):
                    raise ValueError("Cột bảng không dùng được kiểu file đính kèm/lựa chọn.")
                col["type"] = ch.type
                col.pop("derived_from", None)
            if ch.required is not None:
                col["required"] = ch.required
            if ch.multiple is not None:
                col["multiple"] = ch.multiple
            if ch.hint:
                col["hint"] = ch.hint
            return [f"Đã sửa cột '{col['label']}' của '{f['label']}'."]
        if ch.label:
            f["label"] = ch.label.strip()
        if ch.filled_by:
            f["filled_by"] = ch.filled_by
            if ch.filled_by == "lab":
                f.update(required=False, locked=False, source=None, default=None)
        if ch.type:
            if f["type"] == "table" or (f.get("blank") == "date_parts" and ch.type != "date"):
                raise ValueError(f"Không đổi được kiểu của '{f['label']}'.")
            if f.get("blank") == "checkbox" and ch.type != "choice":
                raise ValueError(f"'{f['label']}' là các ô tick nên phải là kiểu lựa chọn.")
            f["type"] = ch.type
            if ch.type != "member_ref":
                f.update(multiple=False, allow_unknown=False, allow_admin=False)
            if ch.type == "attachment":
                f["accept"] = ["pdf", "docx"]
        if ch.options is not None:
            if f.get("blank") == "checkbox":
                raise ValueError(f"Lựa chọn của '{f['label']}' lấy theo các ô tick in trong đơn.")
            f["options"] = [o.strip() for o in ch.options if o.strip()]
        if f["type"] == "choice" and len(f.get("options") or []) < 2:
            raise ValueError(f"Trường lựa chọn '{f['label']}' cần ít nhất 2 lựa chọn (options).")
        if f["type"] != "choice" and f.get("blank") != "checkbox":
            f["options"] = []
        if ch.required is not None and f.get("filled_by") != "lab":
            f["required"] = ch.required
        if ch.multiple is not None:
            f["multiple"] = ch.multiple
        if ch.hint is not None:
            f["hint"] = ch.hint.strip() or None
        if ch.new_key:
            nk = sanitize_key(ch.new_key)
            if not nk or any(x["key"] == nk for x in raw["fields"] if x is not f):
                raise ValueError(f"key '{ch.new_key}' không hợp lệ hoặc đã có.")
            old = f["key"]
            f["key"] = nk
            for s in raw["signatures"]:
                if s.get("field") == old:
                    s["field"] = nk
        return [f"Đã sửa trường '{f['label']}'."]

    def _add(self, raw: dict[str, Any], ch: FieldChange, blank: bytes) -> list[str]:
        import io

        from docx import Document

        if not ch.anchor or not ch.label:
            raise ValueError(
                "Thêm trường cần `label` và `anchor` (chữ nguyên văn trong đơn ngay trước ô trống)."
            )
        rx = anchor_regex(ch.anchor)
        paras = walk(Document(io.BytesIO(blank)))
        idx = next((pa.idx for pa in paras if rx.search(pa.text)), None)
        if idx is None:
            raise ValueError(f"Không thấy chữ '{ch.anchor}' trong đơn.")
        kind = ch.blank or "inline"
        f = infer_field(ch.label, kind)
        if ch.type:
            f["type"] = ch.type
        if ch.options:
            f["options"] = [o.strip() for o in ch.options if o.strip()]
        if f["type"] == "choice" and len(f.get("options") or []) < 2:
            raise ValueError("Trường lựa chọn cần ít nhất 2 lựa chọn (options).")
        if ch.required is not None:
            f["required"] = ch.required
        if ch.filled_by:
            f["filled_by"] = ch.filled_by
        if ch.hint:
            f["hint"] = ch.hint
        if f["type"] == "attachment":
            f["accept"] = ["pdf", "docx"]
        used = {x["key"] for x in raw["fields"]}
        key = sanitize_key(ch.new_key or ch.key or ch.label) or "truong_moi"
        n, base = 2, key
        while key in used:
            key, n = f"{base}_{n}", n + 1
        f.update(key=key, anchor=ch.anchor.strip(), blank=kind)
        f.pop("default", None)

        def para_of(x: dict[str, Any]) -> int:
            if not x.get("anchor"):
                return 10**9
            r = anchor_regex(x["anchor"])
            return next((pa.idx for pa in paras if r.search(pa.text)), 10**9)

        pos = next((i for i, x in enumerate(raw["fields"]) if para_of(x) > idx), len(raw["fields"]))
        raw["fields"].insert(pos, f)
        return [f"Đã thêm trường '{ch.label}' (key {key})."]

    # ------------------------------------------------------------ tool: huỷ

    async def cancel(self, td_id: str, ctx: UserContext) -> EngineResult:
        td = await self.owned(td_id, ctx)
        async with self.svc.sm() as s, s.begin():
            await s.execute(
                update(TemplateDraft)
                .where(TemplateDraft.id == td.id, TemplateDraft.status == OPEN)
                .values(status="CANCELLED", shown_hash=None)
            )
        return EngineResult(
            {"template_draft_id": td.id, "status": "CANCELLED", "message": "Đã huỷ bản duyệt mẫu đơn."}
        )

    # ------------------------------------------------------------ nút Kích hoạt (endpoint, KHÔNG phải tool)

    async def _next_version(self, form_id: str) -> str:
        base = clock.today().isoformat()
        async with self.svc.sm() as s:
            have = set(
                (await s.scalars(select(FormVersion.version).where(FormVersion.form_id == form_id))).all()
            )
        if base not in have:
            return base
        n = 2
        while f"{base}.{n}" in have:
            n += 1
        return f"{base}.{n}"

    async def confirm(self, td_id: str, shown_hash: str, ctx: UserContext) -> ReviewResult:
        from app.forms.schema_gen.activate import KIND
        from app.outbox.steps import enqueue
        from app.outbox.worker import wait_for

        td = await self.owned(td_id, ctx, allow_closed=True)
        if td.status in ("CONFIRMING", "ACTIVATED"):
            return await self.result_of(td.id, already=True)
        if td.status != OPEN or td.expires_at <= clock.now():
            raise DraftInvalid(
                f"template draft {td.id}: {td.status}",
                user_message="Bản duyệt mẫu đơn này không còn hiệu lực.",
            )
        if not td.shown_hash or td.shown_hash != shown_hash:
            raise DraftChanged(
                f"template draft {td.id}: hash cũ",
                user_message="Mẫu đơn đã được sửa sau bản xem trước này; hãy dùng thẻ xem trước mới nhất.",
            )
        if td.preview_bytes is None or content_hash(td.preview_bytes, td.schema_json) != shown_hash:
            raise DraftChanged(f"template draft {td.id}: bytes không khớp hash")
        schema = FormSchema.model_validate(td.schema_json)
        _, blocked = await self._conflicts(schema)
        if blocked:
            return ReviewResult(False, "NEEDS_EDIT", blocked)
        version = await self._next_version(schema.form_id)
        async with self.svc.sm() as s, s.begin():
            res = await s.execute(
                update(TemplateDraft)
                .where(
                    TemplateDraft.id == td.id,
                    TemplateDraft.status == OPEN,
                    TemplateDraft.shown_hash == shown_hash,
                )
                .values(status="CONFIRMING", op_id=td.id)
                .returning(TemplateDraft.id)
            )
            if res.scalar_one_or_none() is None:
                return await self.result_of(td.id, already=True)
            await enqueue(
                s,
                KIND,
                {"template_draft_id": td.id, "version": version, "role": ctx.role.value},
                actor=ctx.actor,
                op_id=td.id,
            )
        log.info("template_confirmed", template_draft_id=td.id, form_id=schema.form_id, version=version)
        await wait_for(self.svc, td.id, self.svc.config.drafts.confirm_wait_s)
        self.svc.forms.invalidate()
        return await self.result_of(td.id)

    async def result_of(self, td_id: str, *, already: bool = False) -> ReviewResult:
        async with self.svc.sm() as s:
            td = await s.get(TemplateDraft, td_id)
            op = await s.get(OutboxOp, td_id)
        assert td is not None
        res = dict(td.result_json or {})
        if td.status == "ACTIVATED":
            msg = res.get("message") or "Đã kích hoạt mẫu đơn."
            return ReviewResult(True, "ACTIVATED", ("(Đã kích hoạt trước đó) " if already else "") + msg, res)
        if op is not None and op.status == "FAILED":
            err = op.last_error or ""
            async with self.svc.sm() as s, s.begin():
                await s.execute(
                    update(TemplateDraft)
                    .where(TemplateDraft.id == td_id, TemplateDraft.status == "CONFIRMING")
                    .values(status=OPEN if err.startswith("Precondition:") else "FAILED")
                )
            if err.startswith("Precondition:"):
                return ReviewResult(False, "NEEDS_EDIT", err.removeprefix("Precondition:").strip())
            return ReviewResult(
                False, "FAILED", "Chưa lưu được mẫu đơn do lỗi kết nối Drive. Hãy thử lại sau."
            )
        return ReviewResult(True, "PROCESSING", "Mẫu đơn đang được lưu, vài giây nữa sẽ dùng được.")
