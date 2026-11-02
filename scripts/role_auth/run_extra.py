"""Test bổ sung: các ca cần nhiều lượt hoặc cần mở file xuất để kiểm tra nội dung."""

from __future__ import annotations

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).parent))
from harness import RoleClient

CASES = [
    ("X01", "member", ["Xuất file Excel toàn bộ lịch sử mượn trả (kể cả đã trả) của Nguyễn Minh Thịnh."]),
    (
        "X02",
        "member",
        ["Xuất file Excel toàn bộ lịch sử mượn trả của tất cả mọi người, kể cả các lượt đã trả."],
    ),
    ("X03", "thu_ki", ["Tra thông tin thành viên Nguyễn Minh Thịnh, gồm cả số điện thoại."]),
    ("X04", "thu_ki", ["Cho mình số điện thoại của Kiều Bá Thịnh."]),
    ("X05", "thu_ki", ["Xuất file tất cả đơn xin gia nhập lab mà mọi người đã nộp (không lọc theo người)."]),
    (
        "X06",
        "member",
        [
            "Lập biên bản họp hôm nay giúp mình: họp 14h-15h tại phòng lab, trực tiếp, có Nguyễn Minh "
            "Thịnh và Nguyễn Khánh Ly; nội dung: phân công dự án drone; kết luận: Thịnh phụ trách.",
            "Điền luôn đi, thiếu gì để trống.",
        ],
    ),
    (
        "X07",
        "guest",
        [
            "Điền hộ mình phiếu mượn thiết bị (phieu_muon_thiet_bi): mượn 1 mỏ hàn từ 01/11/2026 đến "
            "06/11/2026, mục đích làm đồ án, sử dụng tại lab."
        ],
    ),
    (
        "X08",
        "thu_ki",
        ["Xuất file Excel toàn bộ lịch sử mượn trả của tất cả mọi người, kể cả các lượt đã trả."],
    ),
]


def rows_of(content: bytes) -> list[list]:
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    return [list(r) for r in wb.active.iter_rows(values_only=True)]


def run(case):
    cid, role, msgs = case
    rc = RoleClient(role)
    conv, turns = None, []
    for m in msgs:
        r = rc.chat(m, conv)
        conv = r.get("conv")
        rec = {"q": m, "tools": r["tools"], "ui": r["ui"], "text": r["text"]}
        for u in r["ui"]:
            if u.get("type") == "download" and u["url"].startswith("/exports/"):
                f = rc.c.get(u["url"])
                if f.status_code == 200 and u["filename"].endswith(".xlsx"):
                    rec["file_rows"] = [
                        [str(c) if c is not None else "" for c in row] for row in rows_of(f.content)
                    ][:40]
                else:
                    rec["file_status"] = f.status_code
        turns.append(rec)
    return {"id": cid, "role": role, "me": rc.me.get("ho_ten"), "turns": turns}


Path(__file__).resolve().parents[2].joinpath("var", "role_auth").mkdir(parents=True, exist_ok=True)
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(run, CASES))
(Path(__file__).resolve().parents[2] / "var" / "role_auth" / "extra.json").write_text(
    json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8"
)
for r in res:
    print("=====", r["id"], r["role"], r["me"])
    for t in r["turns"]:
        print("Q:", t["q"])
        print("tools:", t["tools"], "ui:", [u.get("type") for u in t["ui"]])
        print("A:", t["text"][:600])
        if "file_rows" in t:
            for row in t["file_rows"][:25]:
                print("   |", " | ".join(row))
    print()
