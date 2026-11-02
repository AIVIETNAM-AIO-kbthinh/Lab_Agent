"""Luồng lịch trực + báo cáo tuần: tạo tab → mời đăng ký → nhắc → báo cáo cho leader.

Ngày cố định: thứ 6 16/10/2026 → tuần sau 19/10–25/10. Email người nhận lấy từ Members giả.
"""

from __future__ import annotations

import pytest

from app.jobs.base import run_job
from tests.conftest import Env

pytestmark = pytest.mark.db


def _mark(env: Env, name: str, cols: dict[int, str]) -> None:
    """Đánh dấu ô (cột 1-based) ở dòng của `name` trong tab tuần sau."""
    grid = env.lab.sheets.values(env.lab.duty_id, env.lab.next_week_tab())
    row = next(r for r in grid if len(r) > 1 and r[1] == name)
    for c, v in cols.items():
        while len(row) < c:
            row.append("")
        row[c - 1] = v


async def test_announce_needs_tab(env: Env) -> None:
    res = await run_job(env.svc, "duty_announce")
    assert res["status"] == "no_tab" and res["week"] == "19/10-25/10"
    assert env.gmail.sent == []


async def test_announce_all_members_once(env: Env) -> None:
    await run_job(env.svc, "duty_create")
    dry = await run_job(env.svc, "duty_announce", dry_run=True)
    assert len(dry["sent"]) == 4 and env.gmail.sent == []
    first = dry["previews"][0]
    assert first["to"] == "leader@gmail.com" and first["subject"].startswith("[TEST]")
    # T không chép từ tuần trước nữa: member chỉ đánh V, agent giao T sau
    assert 'đánh "V" vào các buổi bạn CÓ THỂ lên trực' in first["body"]
    assert "đã được giao" not in first["body"]
    assert "hết Chủ nhật 18/10" in first["body"]
    grid = env.lab.sheets.values(env.lab.duty_id, env.lab.next_week_tab())
    assert not any(c == "T" for row in grid[6:] for c in row[2:])

    r1 = await run_job(env.svc, "duty_announce")
    assert r1["sent"] == ["Nguyễn Văn Leader", "Trần Thị Thư", "Lê Văn An", "Phạm Thị Bình"]
    r2 = await run_job(env.svc, "duty_announce")
    assert r2["sent"] == [] and len(r2["already_sent"]) == 4
    to = sorted(str(m["To"]) for m in env.gmail.sent)
    assert any("an@gmail.com" in x for x in to) and any("binh@vnu.edu.vn" in x for x in to)
    assert all("Mở đăng ký lịch trực tuần 19/10" in s for s in env.gmail.subjects())

    # gửi lại cho đúng một người khi cần test lại
    r3 = await run_job(env.svc, "duty_announce", params={"only": "lê văn an", "resend": "1"})
    assert r3["sent"] == ["Lê Văn An"] and len(env.gmail.sent) == 5


async def test_announce_member_without_email(env: Env) -> None:
    members = env.lab.sheets.values(env.lab.members_id, "Members")
    members[-1][7] = ""  # Phạm Thị Bình mất email
    await run_job(env.svc, "duty_create")
    res = await run_job(env.svc, "duty_announce")
    assert res["no_email"] == ["Phạm Thị Bình"] and len(res["sent"]) == 3
    assert any(
        r[3] == "duty_announce" and r[4] == "Phạm Thị Bình" and r[5] == "FAILED" for r in env.lab.audit_rows()
    )


async def test_remind_counts_and_new_member(env: Env) -> None:
    await run_job(env.svc, "duty_create")
    # An đủ 3 buổi, Thư 1 buổi; thêm thành viên mới vào Members sau khi tab đã tạo
    _mark(env, "Lê Văn An", {3: "v", 5: "V", 7: "v"})
    _mark(env, "Trần Thị Thư", {8: "v"})
    env.lab.sheets.values(env.lab.members_id, "Members").append(
        [5, "SV005", "Vũ Thị Mới", "K69", "", "", "UET", "moi@gmail.com", "Member"]
    )
    dry = await run_job(env.svc, "duty_remind", dry_run=True, params={"only": "Trần Thị Thư"})
    assert dry["sent"] == ["Trần Thị Thư"]
    assert "1/3 buổi: Thứ 4 (21/10) Chiều" in dry["previews"][0]["body"]

    res = await run_job(env.svc, "duty_remind")
    assert "Lê Văn An" not in res["sent"]
    assert sorted(res["sent"]) == sorted(["Nguyễn Văn Leader", "Trần Thị Thư", "Phạm Thị Bình", "Vũ Thị Mới"])
    assert res["missing_rows"] == ["Vũ Thị Mới"]
    moi = next(b for m, b in zip(env.gmail.sent, env.gmail.bodies(), strict=True) if "moi@" in str(m["To"]))
    assert "chưa có trong tab lịch trực" in moi

    # nhắc đợt 2 là khoá riêng; đợt 1 chạy lại không gửi trùng
    assert (await run_job(env.svc, "duty_remind"))["sent"] == []
    r2 = await run_job(env.svc, "duty_remind", params={"round": "2"})
    assert len(r2["sent"]) == 4


def _cell(env: Env, name: str, col: int) -> str:
    grid = env.lab.sheets.values(env.lab.duty_id, env.lab.next_week_tab())
    row = next(r for r in grid if len(r) > 1 and r[1] == name)
    return str(row[col - 1]) if len(row) >= col else ""


def _fill_v(env: Env) -> None:
    """Cột 3 = T2 sáng, 4 = T2 chiều, 5 = T3 sáng, 7 = T4 sáng. Bình không đăng ký buổi nào."""
    _mark(env, "Nguyễn Văn Leader", {3: "V", 4: "v"})
    _mark(env, "Trần Thị Thư", {4: "V", 5: "V"})
    _mark(env, "Lê Văn An", {3: "v", 5: "V", 7: "v"})


async def test_duty_assign_job(env: Env) -> None:
    assert (await run_job(env.svc, "duty_assign"))["status"] == "no_tab"
    await run_job(env.svc, "duty_create")
    _fill_v(env)
    dry = await run_job(env.svc, "duty_assign", dry_run=True)
    assert _cell(env, "Lê Văn An", 7) == "v"  # dry-run không ghi sheet
    res = await run_job(env.svc, "duty_assign")
    assert (
        res["assigned"]
        == dry["assigned"]
        == [
            {"name": "Nguyễn Văn Leader", "slot": "Thứ 2 (19/10) Sáng"},
            {"name": "Trần Thị Thư", "slot": "Thứ 2 (19/10) Chiều"},
            {"name": "Trần Thị Thư", "slot": "Thứ 3 (20/10) Sáng"},
            {"name": "Lê Văn An", "slot": "Thứ 4 (21/10) Sáng"},
        ]
    )
    assert res["no_slot"] == ["Phạm Thị Bình"] and len(res["uncovered"]) == 10
    assert _cell(env, "Lê Văn An", 7) == "T" and _cell(env, "Lê Văn An", 3) == "v"
    assert any(r[3] == "duty_assign" and r[5] == "OK" for r in env.lab.audit_rows())
    assert (await run_job(env.svc, "duty_assign"))["assigned"] == []  # chạy lại không giao thêm


async def test_weekly_report_assigns_then_reports(env: Env) -> None:
    await run_job(env.svc, "duty_create")
    _fill_v(env)
    dry = await run_job(env.svc, "weekly_report", dry_run=True)
    assert sorted(dry["to"]) == ["leader@gmail.com", "thu@gmail.com"]  # leader + thư ký
    assert len(dry["assignment"]["assigned"]) == 4
    assert _cell(env, "Lê Văn An", 7) == "v"  # dry-run chỉ tính phương án, không ghi
    body = dry["previews"][0]["body"]
    # lịch đã giao: (T) = trực chính thức, tên không kèm (T) = mới đăng ký V
    assert "Giao trực tự động: 4 buổi T mới" in body
    assert "Số buổi T / đăng ký: Nguyễn Văn Leader 1/2; Trần Thị Thư 2/2; Lê Văn An 1/3" in body
    assert (
        "Thứ 2 (19/10): Sáng: Nguyễn Văn Leader (T), Lê Văn An | Chiều: Nguyễn Văn Leader, Trần Thị Thư (T)"
        in body
    )
    assert "chưa giao được T vì chưa đăng ký buổi nào: Phạm Thị Bình" in body
    assert "buổi không ai đăng ký nên chưa có người trực T: Thứ 2 (19/10) Chiều" not in body
    assert "Thứ 3 (20/10) Chiều" in body.split("CẦN XỬ LÝ")[1]
    assert "Chưa đủ 3 buổi (3 người)" in body and "Lê Văn An — " not in body.split("2. THIẾT BỊ")[0]
    # ai đang mượn gì (kể cả tên lạ), đến hạn trả trong tuần, quá hạn
    assert "THIẾT BỊ ĐANG ĐƯỢC MƯỢN (3 lượt, 3 người)" in body
    assert "+ Arduino Uno (TB002) ×3 — mượn 06/10 — hạn 12/10 — QUÁ HẠN 4 ngày" in body
    assert "ĐẾN HẠN TRẢ TỪ 16/10 ĐẾN 25/10 (1)" in body
    assert "- 17/10 — Lê Văn An — Máy chiếu Epson (TB001) ×1" in body
    assert "THIẾT BỊ QUÁ HẠN TRẢ (1)" in body

    sent = await run_job(env.svc, "weekly_report")
    assert sent["status"] == "sent" and len(sent["assignment"]["assigned"]) == 4
    assert _cell(env, "Lê Văn An", 7) == "T"  # chạy thật: ghi T lên sheet trước khi gửi
    assert (await run_job(env.svc, "weekly_report"))["status"] == "already_sent"
    again = await run_job(env.svc, "weekly_report", params={"resend": "1"})
    assert again["status"] == "sent" and again["assignment"]["assigned"] == []
    assert len(env.gmail.sent) == 2


async def test_weekly_report_no_leader_email_alerts_admin(env: Env) -> None:
    members = env.lab.sheets.values(env.lab.members_id, "Members")
    members[3][7] = ""  # leader (dòng dữ liệu đầu tiên) không có email → vẫn gửi thư ký
    dry = await run_job(env.svc, "weekly_report", dry_run=True)
    assert dry["to"] == ["thu@gmail.com"]
    members[4][7] = "sai-email"  # thư ký cũng không có email hợp lệ → báo admin
    res = await run_job(env.svc, "weekly_report")
    assert res["status"] == "no_recipient"
    assert any("Không gửi được báo cáo tuần" in s for s in env.gmail.subjects())
