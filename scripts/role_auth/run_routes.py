"""Ma trận route HTTP × role (authentication_plan.md §2). Không gọi LLM, không ghi dữ liệu thật:
POST quản trị gửi body sai để chứng minh đã qua lớp quyền (admin → 422) mà không thực thi."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import RoleClient

ROLES = ["guest", "member", "thu_ki", "leader", "admin"]
ADMIN = {"admin"}
LA = {"leader", "admin"}
# (method, path, json, roles_được_phép, mã_khi_được_phép)
CASES = [
    ("GET", "/admin/api/kb/web", None, ADMIN, {200}),
    ("GET", "/admin/api/ops", None, ADMIN, {200}),
    ("GET", "/admin/api/llm/costs", None, ADMIN, {200}),
    ("GET", "/admin/api/audit", None, ADMIN, {200}),
    ("GET", "/admin/api/accounts", None, ADMIN, {200}),
    ("POST", "/admin/api/accounts", {}, ADMIN, {422}),
    ("POST", "/admin/api/accounts/action", {}, ADMIN, {422}),
    ("POST", "/admin/api/kb/web/crawl", {"mode": "xxx"}, ADMIN, {422}),
    ("POST", "/admin/api/kb/web/runs/999999/apply", None, ADMIN, {400, 404, 409}),
    ("GET", "/admin/api/templates", None, LA, {200}),
    ("GET", "/admin/api/templates/khong-ton-tai/preview", None, LA, {404, 410}),
    ("POST", "/admin/api/templates/forms/khong_ton_tai_xyz/deactivate", None, LA, {404}),
    ("POST", "/admin/api/templates/khong-ton-tai/cancel", None, LA, {404, 410}),
    ("POST", "/auth/change-password", {"old_password": "x", "new_password": "y"}, set(), set()),
    ("GET", "/admin/kb/web", None, LA, {200}),  # trang HTML (G4: hiện ai cũng mở được)
]

rows = []
fails = 0
for role in ROLES:
    rc = RoleClient(role)
    for method, path, body, allowed, ok_codes in CASES:
        r = rc.c.request(method, path, json=body, headers=rc.h())
        if role in allowed:
            good = r.status_code in ok_codes
        else:
            good = r.status_code in (401, 403, 404) or (
                path == "/auth/change-password" and r.status_code >= 400
            )
        if path == "/admin/kb/web" and role not in allowed and r.status_code == 200:
            good = False
        fails += not good
        rows.append(
            (role, method, path, r.status_code, "ok" if good else "FAIL", r.text[:90].replace("\n", " "))
        )

# CSRF: POST /chat thiếu token
rc = RoleClient("member")
r = rc.c.post("/chat", json={"message": "hi"})
rows.append(
    (
        "member",
        "POST",
        "/chat (thiếu CSRF)",
        r.status_code,
        "ok" if r.status_code == 403 else "FAIL",
        r.text[:90],
    )
)
r = rc.c.post("/chat", json={"message": "hi"}, headers={"X-CSRF-Token": "sai"})
rows.append(
    (
        "member",
        "POST",
        "/chat (sai CSRF)",
        r.status_code,
        "ok" if r.status_code == 403 else "FAIL",
        r.text[:90],
    )
)
# Đổi role qua dev-role với role lạ
r = rc.c.post("/auth/dev-role", json={"role": "superadmin"}, headers=rc.h())
rows.append(
    (
        "member",
        "POST",
        "/auth/dev-role role lạ",
        r.status_code,
        "ok" if r.status_code == 422 else "FAIL",
        r.text[:90],
    )
)
# Cookie la_dev_role giả → hạ về mặc định, không lên admin
rc.c.cookies.set("la_dev_role", "root")
me = rc.c.get("/auth/me").json()
rows.append(
    ("cookie=root", "GET", "/auth/me", 200, "ok" if me["role"] != "admin" else "FAIL", f"role={me['role']}")
)

for row in rows:
    print(f"{row[4]:4} {row[0]:12} {row[1]:4} {row[2]:58} {row[3]}  {row[5]}")
print("\nFAIL:", sum(1 for r in rows if r[4] == "FAIL"), "/", len(rows))
