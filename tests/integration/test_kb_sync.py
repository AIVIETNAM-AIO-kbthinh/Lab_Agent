"""kb_sync / kb_reconcile với Drive giả: index kb/, bỏ data/ và tên bắt đầu bằng _, xoá khi chuyển vào _archive."""

from __future__ import annotations

import io

import pytest
from docx import Document

from app.google.drive import DriveChange, DriveFile
from app.jobs.base import run_job
from app.rag.drive_source import drive_doc_id
from tests.conftest import Env

pytestmark = pytest.mark.db


def docx(text: str) -> bytes:
    d = Document()
    d.add_heading("Tài liệu", level=1)
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


async def test_initial_sync_reconciles_and_classifies(env: Env) -> None:
    pub = env.lab.add_kb_doc("kb/public/Nội quy lab.docx", docx("Giờ ra vào 8:00–21:00."))
    mm = env.lab.add_kb_doc(
        "kb/internal/meeting-minutes/2026-10-12_BienBanHop.docx",
        docx("Hà Nội, ngày 12 tháng 10 năm 2026. Họp về UAV."),
    )
    env.lab.add_kb_doc("kb/internal/_nhap.docx", docx("bản nháp"))
    r = await run_job(env.svc, "kb_sync")
    assert r["status"] == "initialized" and r["reconcile"]["indexed"] == 2
    payloads = {p.doc_id: p.payload for p in env.store.points.values()}
    assert payloads[drive_doc_id(pub.id)]["access"] == "public"
    assert payloads[drive_doc_id(pub.id)]["doc_type"] == "rule"
    assert payloads[drive_doc_id(mm.id)]["access"] == "member"
    assert payloads[drive_doc_id(mm.id)]["doc_date"] == "2026-10-12"
    # chạy lại reconcile: không có gì đổi ⇒ không embed lại
    calls = env.embedder.calls
    again = await run_job(env.svc, "kb_reconcile")
    assert again["unchanged"] == 2 and env.embedder.calls == calls


async def test_changes_update_and_archive(env: Env) -> None:
    f = env.lab.add_kb_doc("kb/public/Giới thiệu.docx", docx("Phiên bản 1"))
    await run_job(env.svc, "kb_sync")
    did = drive_doc_id(f.id)
    assert did in env.store.docs()
    # sửa nội dung
    env.lab.drive.content[f.id] = docx("Phiên bản 2 có nội dung mới")
    f.version = "2"
    env.lab.drive.changes = [DriveChange(f.id, False, f)]
    r = await run_job(env.svc, "kb_sync")
    assert r.get("indexed") == 1
    assert any("Phiên bản 2" in p.payload["text"] for p in env.store.points.values() if p.doc_id == did)
    # chuyển vào _archive ⇒ xoá chunk
    archive = DriveFile("D_archive", "_archive", "application/vnd.google-apps.folder", ["ROOT"])
    env.lab.drive.files[archive.id] = archive
    f.parents = [archive.id]
    env.lab.drive.changes = [DriveChange(f.id, False, f)]
    r = await run_job(env.svc, "kb_sync")
    assert r.get("deleted") == 1 and did not in env.store.docs()


async def test_data_sheets_never_indexed(env: Env) -> None:
    members = env.lab.drive.files["S_MEMBERS"]
    await run_job(env.svc, "kb_sync")
    env.lab.drive.changes = [DriveChange(members.id, False, members)]
    r = await run_job(env.svc, "kb_sync")
    assert r.get("indexed") is None and drive_doc_id(members.id) not in env.store.docs()
