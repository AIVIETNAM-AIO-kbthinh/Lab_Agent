"""Tài liệu KB trên Drive: kb/public/** → access=public, kb/internal/** → access=member (TK §2.1, §6.2).

Tên bắt đầu bằng `_` (file, folder) bị bỏ qua. Sheet trong data/ không bao giờ được index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import IndexCfg
from app.domain.names import ascii_key
from app.google.drive import DOCX, GDOC, PDF, DriveAPI, DriveFile
from app.google.paths import PathResolver
from app.rag.extract_text import Extracted, from_docx, from_pdf, from_plain
from app.rag.indexer import SourceDoc, doc_id_for

SUPPORTED = {GDOC, DOCX, PDF, "text/plain", "text/markdown"}


@dataclass
class KbFile:
    file: DriveFile
    path: str  # tương đối từ root, vd kb/internal/meeting-minutes/2026-10-12_BienBanHop
    access: Literal["public", "member"]


def classify_path(path: str, cfg: IndexCfg) -> Literal["public", "member"] | None:
    parts = path.strip("/").split("/")
    if any(p.startswith("_") for p in parts):
        return None
    pub, internal = cfg.kb_public_path.strip("/"), cfg.kb_internal_path.strip("/")
    if path == pub or path.startswith(pub + "/"):
        return "public"
    if path == internal or path.startswith(internal + "/"):
        return "member"
    return None


def doc_type_for(path: str, name: str) -> str:
    p = ascii_key(path)
    if "meeting-minutes" in p or "bien ban" in p or "bienban" in p:
        return "minutes"
    n = ascii_key(name)
    if any(k in n for k in ("noi quy", "noiquy", "quy dinh", "quydinh", "quy che", "rule")):
        return "rule"
    return "other"


def drive_doc_id(file_id: str) -> str:
    return doc_id_for("drive", file_id)


async def walk_kb(drive: DriveAPI, paths: PathResolver, cfg: IndexCfg) -> list[KbFile]:
    """Liệt kê đệ quy mọi file KB hợp lệ (dùng cho kb_reconcile)."""
    out: list[KbFile] = []
    for base in (cfg.kb_public_path, cfg.kb_internal_path):
        try:
            root = await paths.resolve(base)
        except Exception:  # noqa: S112 - thư mục kb chưa tạo thì bỏ qua
            continue
        stack = [(root, base.strip("/"))]
        while stack:
            folder, fpath = stack.pop()
            for f in await drive.list_children(folder.id):
                p = f"{fpath}/{f.name}"
                access = classify_path(p, cfg)
                if access is None:
                    continue
                if f.is_folder:
                    stack.append((f, p))
                elif f.mime_type in SUPPORTED:
                    out.append(KbFile(f, p, access))
    return out


async def extract(drive: DriveAPI, f: DriveFile) -> Extracted | None:
    title = f.name.rsplit(".", 1)[0] if f.mime_type != GDOC else f.name
    if f.mime_type == GDOC:
        return from_docx(await drive.export(f.id, DOCX), title)
    if f.mime_type == DOCX:
        return from_docx(await drive.download(f.id), title)
    if f.mime_type == PDF:
        return from_pdf(await drive.download(f.id), title)
    if f.mime_type in {"text/plain", "text/markdown"}:
        return from_plain(await drive.download(f.id), title)
    return None


async def to_source_doc(drive: DriveAPI, kb: KbFile) -> SourceDoc | None:
    ex = await extract(drive, kb.file)
    if ex is None:
        return None
    return SourceDoc(
        doc_id=drive_doc_id(kb.file.id),
        source="drive",
        location=kb.path,
        title=ex.title or kb.file.name,
        access=kb.access,
        doc_type=doc_type_for(kb.path, kb.file.name),
        markdown=ex.markdown,
        lang="vi",
        doc_date=ex.doc_date if doc_type_for(kb.path, kb.file.name) == "minutes" else None,
        source_version=kb.file.version or kb.file.modified_time,
    )
