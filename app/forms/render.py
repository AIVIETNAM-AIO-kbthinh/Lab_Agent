"""Render đơn: template (đã có placeholder) + giá trị → docx (docxtpl) → PDF (LibreOffice headless).

- Không tạo file nào trên Drive trước khi xác nhận (TK §7.4).
- LibreOffice: profile tạm riêng mỗi lần, giới hạn số tiến trình đồng thời, timeout và kill nếu treo.
- Máy không có LibreOffice (dev): bản xem trước là chính file DOCX; hash vẫn tính trên bytes đã hiển thị.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import anyio.to_thread
from docxtpl import DocxTemplate, Listing

from app.config import RenderCfg
from app.domain.dates import parse_sheet_date
from app.errors import RenderFailed
from app.forms.schema import FormSchema
from app.google.drive import DOCX, GDOC, PDF, DriveAPI
from app.logging import get_logger

log = get_logger(__name__)

WINDOWS_SOFFICE = (
    Path("C:/Program Files/LibreOffice/program/soffice.exe"),
    Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
)


class PdfConverter(Protocol):
    @property
    def available(self) -> bool: ...

    async def to_pdf(self, docx: bytes) -> bytes: ...


def find_soffice(configured: str | None = None) -> str | None:
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for path in WINDOWS_SOFFICE:
        if path.exists():
            return str(path)
    return None


class SofficeConverter:
    def __init__(self, cfg: RenderCfg) -> None:
        self.cfg = cfg
        self.binary = find_soffice(cfg.soffice_path)
        self._sem = asyncio.Semaphore(max(1, cfg.max_concurrency))

    @property
    def available(self) -> bool:
        return self.binary is not None

    def _run(self, docx: bytes) -> bytes:
        assert self.binary is not None
        with tempfile.TemporaryDirectory(prefix="labagent-render-") as tmp:
            d = Path(tmp)
            src = d / "form.docx"
            src.write_bytes(docx)
            profile = (d / "profile").as_uri()
            cmd = [
                self.binary,
                f"-env:UserInstallation={profile}",
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--convert-to",
                "pdf",
                "--outdir",
                str(d),
                str(src),
            ]
            try:
                subprocess.run(  # noqa: S603 - đường dẫn binary do config/hệ thống quyết định
                    cmd, check=True, capture_output=True, timeout=self.cfg.timeout_s
                )
            except subprocess.TimeoutExpired as e:
                raise RenderFailed(f"LibreOffice quá {self.cfg.timeout_s}s") from e
            except subprocess.CalledProcessError as e:
                raise RenderFailed(f"LibreOffice lỗi: {e.stderr[:300]!r}") from e
            out = d / "form.pdf"
            if not out.exists():
                raise RenderFailed("LibreOffice không sinh ra file PDF")
            return out.read_bytes()

    async def to_pdf(self, docx: bytes) -> bytes:
        if self.binary is None:
            raise RenderFailed("Không có LibreOffice")
        async with self._sem:
            return await anyio.to_thread.run_sync(self._run, docx)


# ---------------------------------------------------------------- ngữ cảnh docxtpl


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def build_context(
    schema: FormSchema, fields: dict[str, Any], attachments: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any]:
    unknown: dict[str, list[str]] = meta.get("unknown", {})
    ctx: dict[str, Any] = {}
    for f in schema.fields:
        v = fields.get(f.key)
        if f.type == "attachment":
            a = attachments.get(f.key)
            ctx[f.key] = f"đính kèm ({a['filename']})" if a else ""
        elif f.type == "table":
            ctx[f.key] = [{c.key: _s(row.get(c.key)) for c in f.columns} for row in (v or [])]
        elif f.type == "longtext":
            ctx[f.key] = Listing(_s(v))
        elif f.type == "member_ref" and isinstance(v, list):
            marks = set(unknown.get(f.key, []))
            ctx[f.key] = ", ".join(f"{n} (ngoài lab)" if n in marks else n for n in v)
        else:
            ctx[f.key] = _s(v)
        if f.type == "choice" and f.blank == "checkbox":
            chosen = set(v) if isinstance(v, list) else {v}
            for i, o in enumerate(f.options):
                ctx[f"{f.key}_opt{i}"] = "☒" if o in chosen else "☐"
        if f.type == "date":
            d = parse_sheet_date(v) if v else None
            ctx[f"{f.key}_d"] = f"{d.day:02d}" if d else "…"
            ctx[f"{f.key}_m"] = f"{d.month:02d}" if d else "…"
            ctx[f"{f.key}_y"] = str(d.year) if d else "…"
    for i, sig in enumerate(schema.signatures):
        if sig.field:
            ctx[f"sig_{i}"] = _s(fields.get(sig.field))
    return ctx


def summary_lines(
    schema: FormSchema, fields: dict[str, Any], attachments: dict[str, Any], meta: dict[str, Any]
) -> list[list[str]]:
    """Bản tóm tắt dạng text gửi kèm bản xem trước (label, giá trị)."""
    unknown: dict[str, list[str]] = meta.get("unknown", {})
    out: list[list[str]] = []
    for f in schema.fields:
        if f.filled_by == "lab":
            continue
        if f.type == "attachment":
            a = attachments.get(f.key)
            out.append([f.label, a["filename"] if a else "(chưa có)"])
            continue
        v = fields.get(f.key)
        if f.type == "table":
            rows = v or []
            if not rows:
                out.append([f.label, "(trống)"])
            for row in rows:
                cells = [f"{c.label}: {_s(row.get(c.key))}" for c in f.columns if c.type != "row_number"]
                out.append([f"{f.label} #{row.get('stt', '')}".strip(), " · ".join(cells)])
            continue
        text = _s(v)
        if f.type == "member_ref" and isinstance(v, list):
            marks = set(unknown.get(f.key, []))
            text = ", ".join(f"{n} (ngoài lab)" if n in marks else n for n in v)
        out.append([f.label, text or "—"])
    return out


@dataclass
class RenderResult:
    docx: bytes
    preview: bytes
    mime: str  # application/pdf hoặc DOCX khi không có LibreOffice

    @property
    def ext(self) -> str:
        return "pdf" if self.mime == PDF else "docx"


def render_docx(template: bytes, context: dict[str, Any]) -> bytes:
    tpl = DocxTemplate(io.BytesIO(template))
    tpl.render(context, autoescape=True)
    out = io.BytesIO()
    tpl.save(out)
    return out.getvalue()


class Renderer:
    def __init__(self, converter: PdfConverter) -> None:
        self.converter = converter

    async def render(
        self,
        template: bytes,
        schema: FormSchema,
        fields: dict[str, Any],
        attachments: dict[str, Any],
        meta: dict[str, Any],
    ) -> RenderResult:
        ctx = build_context(schema, fields, attachments, meta)
        try:
            docx = await anyio.to_thread.run_sync(render_docx, template, ctx)
        except Exception as e:
            raise RenderFailed(f"docxtpl: {type(e).__name__}: {e}") from e
        if not self.converter.available:
            log.warning("render_no_pdf_converter", form_id=schema.form_id)
            return RenderResult(docx, docx, DOCX)
        pdf = await self.converter.to_pdf(docx)
        return RenderResult(docx, pdf, PDF)


# ---------------------------------------------------------------- template trên Drive (cache cục bộ)


class TemplateStore:
    """Bytes template theo file_id Drive. File trong _system/ không bao giờ bị sửa (mỗi lần sửa = version mới)
    nên cache theo file_id là an toàn."""

    def __init__(self, drive: DriveAPI, cache_dir: Path) -> None:
        self.drive = drive
        self.cache_dir = cache_dir
        self._mem: dict[str, bytes] = {}

    async def get(self, file_id: str) -> bytes:
        if file_id in self._mem:
            return self._mem[file_id]
        p = self.cache_dir / f"{file_id}.docx"
        if p.exists():
            data = p.read_bytes()
        else:
            f = await self.drive.get_file(file_id)
            data = await (
                self.drive.export(file_id, DOCX) if f.mime_type == GDOC else self.drive.download(file_id)
            )
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            except OSError as e:
                log.warning("template_cache_write_failed", error=str(e)[:200])
        self._mem[file_id] = data
        return data

    async def blank(self, file_id: str) -> tuple[bytes, str]:
        """Đơn trống gốc để gửi user (Google Doc → xuất docx). Trả (bytes, version)."""
        f = await self.drive.get_file(file_id)
        key = f"blank:{file_id}:{f.version or f.modified_time}"
        if key not in self._mem:
            self._mem[key] = await (
                self.drive.export(file_id, DOCX) if f.mime_type == GDOC else self.drive.download(file_id)
            )
        return self._mem[key], str(f.version or "")


def today_parts(d: date) -> dict[str, str]:
    return {"d": f"{d.day:02d}", "m": f"{d.month:02d}", "y": str(d.year)}
