"""retention (hằng tháng): đơn gia nhập bị từ chối quá N tháng → chuyển folder hồ sơ vào _archive/ (TK §8.1, §9).

Không xoá cứng. Dòng FormSubmissions giữ nguyên trạng thái TU_CHOI, Ghi chú thêm "[đã lưu trữ dd/mm/yyyy]" để chạy
lại không chuyển hai lần. Ngày từ chối đọc từ Ghi chú ("Từ chối ngày dd/mm/yyyy"), không có thì lấy ngày nộp.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.domain.dates import fmt_date, parse_sheet_date
from app.jobs.base import JobContext, job

APP_FORM = "don_xin_gia_nhap"
ARCHIVE = "_archive/applications"
MARK = "[đã lưu trữ"
_FOLDER = re.compile(r"/folders/([\w-]+)")
_REJECTED = re.compile(r"Từ chối ngày (\d{1,2}/\d{1,2}/\d{4})")


def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0)


@job("retention")
async def retention(ctx: JobContext) -> dict[str, Any]:
    svc = ctx.svc
    keep = svc.config.applications.rejected_retention_months
    subs = await svc.submissions.list(APP_FORM)
    due = []
    for s in subs:
        if s.text("trang_thai") != "TU_CHOI" or MARK in s.text("ghi_chu"):
            continue
        m = _REJECTED.search(s.text("ghi_chu"))
        rejected_on = parse_sheet_date(m.group(1)) if m else s.submitted_on
        if rejected_on is None or months_between(rejected_on, ctx.today) < keep:
            continue
        due.append(s)
    moved, missing = [], []
    if due and not ctx.dry_run:
        archive = await svc.paths.ensure_folder(ARCHIVE)
        for s in due:
            fm = _FOLDER.search(s.text("link_file"))
            if fm:
                f = await svc.google.drive.get_file(fm.group(1))
                if archive.id not in f.parents and f.parents:
                    await svc.google.drive.move(f.id, archive.id, f.parents[0])
                moved.append(s.text("submission_id"))
            else:
                missing.append(s.text("submission_id"))
            note = f"{s.text('ghi_chu')} {MARK} {fmt_date(ctx.today)}]".strip()
            await svc.submissions.set_status(APP_FORM, s.text("submission_id"), "TU_CHOI", note)
        await svc.audit.log(
            "system", "system", "retention", APP_FORM, True, {"moved": moved, "no_folder": missing}
        )
    return {
        "due": [s.text("submission_id") for s in due],
        "moved": moved,
        "no_folder": missing,
        "dry_run": ctx.dry_run,
    }
