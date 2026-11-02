"""POST /uploads: kiểm tra magic bytes, đuôi file, kích thước, zip-bomb, macro/đối tượng nhúng trong docx."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import timedelta
from pathlib import Path

import filetype
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app import clock
from app.auth.deps import current_user, get_services, require_csrf
from app.db.models import Upload

router = APIRouter(tags=["uploads"])

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_UNZIPPED = 60 * 1024 * 1024
MAX_ENTRIES = 2000


class UploadRejected(ValueError):
    pass


def inspect_file(data: bytes, filename: str, allowed: list[str], max_mb: int) -> tuple[str, str]:
    """Trả (ext, mime) hoặc ném UploadRejected với lý do an toàn để hiển thị."""
    if len(data) == 0:
        raise UploadRejected("File rỗng.")
    if len(data) > max_mb * 1024 * 1024:
        raise UploadRejected(f"File vượt quá {max_mb} MB.")
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ext not in allowed:
        raise UploadRejected(f"Chỉ nhận file: {', '.join(allowed)}.")
    kind = filetype.guess(data)
    if ext == "pdf":
        if not data.startswith(b"%PDF-") or (kind is not None and kind.extension != "pdf"):
            raise UploadRejected("Nội dung file không phải PDF hợp lệ.")
        return ext, MIME["pdf"]
    if ext == "docx":
        if not data.startswith(b"PK\x03\x04"):
            raise UploadRejected("Nội dung file không phải DOCX hợp lệ.")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = z.infolist()
                if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
                    raise UploadRejected("File DOCX bất thường (quá lớn khi giải nén).")
                names = {i.filename.lower() for i in infos}
                if "[content_types].xml" not in names or not any(
                    n.startswith("word/document") for n in names
                ):
                    raise UploadRejected("Nội dung file không phải DOCX hợp lệ.")
                if any(
                    n.endswith("vbaproject.bin")
                    or "/embeddings/" in n
                    or n.endswith(".bin")
                    or "activex" in n
                    for n in names
                ):
                    raise UploadRejected("File DOCX chứa macro hoặc đối tượng nhúng, không được chấp nhận.")
                ct = z.read("[Content_Types].xml").decode("utf-8", errors="ignore").lower()
                if "macroenabled" in ct:
                    raise UploadRejected("File DOCX chứa macro, không được chấp nhận.")
        except zipfile.BadZipFile:
            raise UploadRejected("File DOCX bị hỏng.") from None
        return ext, MIME["docx"]
    raise UploadRejected("Loại file không được hỗ trợ.")


@router.post("/uploads", dependencies=[Depends(require_csrf)])
async def upload(request: Request, file: UploadFile = File(...)) -> dict[str, object]:
    svc = get_services(request)
    ctx = await current_user(request)
    cfg = svc.config.upload
    data = await file.read(cfg.max_mb * 1024 * 1024 + 1)
    try:
        ext, mime = inspect_file(data, file.filename or "", cfg.allowed_types, cfg.max_mb)
    except UploadRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    digest = hashlib.sha256(data).hexdigest()
    folder = svc.config.var_dir / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    row = Upload(
        session_public_id=ctx.session_id,
        filename=Path(file.filename or f"file.{ext}").name[:255],
        mime=mime,
        ext=ext,
        size=len(data),
        sha256=digest,
        path="",
        expires_at=clock.now() + timedelta(hours=svc.config.retention.upload_hours),
    )
    async with svc.sm() as s, s.begin():
        s.add(row)
        await s.flush()
        path = folder / f"{row.id}.{ext}"
        path.write_bytes(data)
        row.path = str(path)
    return {"upload_id": row.id, "filename": row.filename, "size": row.size, "type": ext}
