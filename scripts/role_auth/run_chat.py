"""Chạy kịch bản chat theo role, kiểm tra tự động, rồi kiểm tra chéo nút Xác nhận và file xuất."""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import RoleClient
from scenarios import S

OUT = Path(__file__).resolve().parents[2] / "var" / "role_auth"
OUT.mkdir(parents=True, exist_ok=True)
only = set(sys.argv[1:])


def judge(sc: dict, r: dict) -> list[str]:
    fails = []
    if r["http"] != 200:
        return [f"HTTP {r['http']}: {r.get('body', '')[:200]}"]
    done = {n for n, st in r["tools"] if st == "done"}
    if sc.get("must") and not (done & set(sc["must"])):
        fails.append(f"không gọi thành công tool nào trong {sc['must']}")
    for t in sc.get("forbid", []):
        if t in done:
            fails.append(f"tool cấm chạy thành công: {t}")
    confirm_ui = [u for u in r["ui"] if u.get("type") != "form_link"]
    if sc.get("ui") is True and not r["ui"]:
        fails.append("thiếu nút (xác nhận/tải)")
    if sc.get("ui") is False and confirm_ui:
        fails.append(f"có nút không được phép: {[u.get('type') for u in confirm_ui]}")
    for pat in sc.get("leak", []):
        m = re.search(pat, r["text"])
        if m:
            fails.append(f"lộ dữ liệu: '{m.group(0)}'")
    for pat in sc.get("must_regex", []):
        if not re.search(pat, r["text"]):
            fails.append(f"thiếu nội dung mong đợi /{pat}/")
    return fails


def run_role(role: str) -> list[dict]:
    rc = RoleClient(role)
    out = []
    for sc in [s for s in S if s["role"] == role and (not only or s["id"] in only)]:
        r = rc.chat(sc["prompt"])
        fails = judge(sc, r)
        rec = {**sc, "result": r, "fails": fails, "me": rc.me.get("ho_ten")}
        # file xuất: chủ tải được, session khác không tải được
        for u in r["ui"]:
            if u.get("type") == "download" and u["url"].startswith("/exports/"):
                own = rc.c.get(u["url"])
                other = RoleClient(role).c.get(u["url"])
                rec.setdefault("export", []).append(
                    {
                        "url": u["url"],
                        "owner_status": own.status_code,
                        "other_session_status": other.status_code,
                    }
                )
                if other.status_code == 200:
                    fails.append("session khác tải được file xuất")
                if own.status_code == 200 and sc.get("check_export") == "member_no_phone":
                    head = next(csv.reader(io.StringIO(own.content.decode("utf-8-sig"))))
                    rec["export"][-1]["header"] = head
                    bad = [
                        h
                        for h in head
                        if re.search(r"SĐT|Phone|MSSV|Student|Điện thoại|Địa chỉ|Address", h, re.I)
                    ]
                    if bad:
                        fails.append(f"file xuất của member có cột nhạy cảm: {bad}")
        out.append(rec)
        print(
            f"[{sc['id']}] {role:7} {'PASS' if not fails else 'FAIL'} tools={r['tools']} ui={[u.get('type') for u in r['ui']]} {fails}",
            flush=True,
        )
    # kiểm tra chéo nút Xác nhận: role khác cùng session → bị chặn; session khác → 404/410; chủ huỷ
    others = {
        "leader": ["thu_ki", "member", "guest"],
        "admin": ["leader", "thu_ki", "member", "guest"],
        "member": ["guest"],
        "thu_ki": ["guest"],
    }
    for rec in out:
        for u in rec["result"]["ui"]:
            if u.get("type") != "action_confirm":
                continue
            aid = u["action_id"]
            checks = []
            for other in others.get(role, []):
                rc.set_role(other)
                resp = rc.c.post(f"/actions/{aid}/confirm", headers=rc.h())
                checks.append({"as": other, "confirm_status": resp.status_code, "body": resp.text[:200]})
                if resp.status_code == 200 and resp.json().get("ok"):
                    rec["fails"].append(f"{other} xác nhận được action {aid}")
            rc.set_role(role)
            stranger = RoleClient(role)
            resp = stranger.c.post(f"/actions/{aid}/confirm", headers=stranger.h())
            checks.append(
                {"as": f"{role} (session khác)", "confirm_status": resp.status_code, "body": resp.text[:200]}
            )
            if resp.status_code == 200:
                rec["fails"].append("session khác xác nhận được action")
            resp = rc.c.post(f"/actions/{aid}/cancel", headers=rc.h())
            checks.append(
                {"as": f"{role} (chủ) huỷ", "cancel_status": resp.status_code, "body": resp.text[:200]}
            )
            rec.setdefault("cross", []).extend(checks)
            print(
                f"   cross {rec['id']} {aid}: {[(c['as'], c.get('confirm_status') or c.get('cancel_status')) for c in checks]}",
                flush=True,
            )
    return out


roles = ["guest", "member", "thu_ki", "leader", "admin"]
with ThreadPoolExecutor(5) as ex:
    results = [x for lst in ex.map(run_role, roles) for x in lst]
tag = "_".join(sorted(only)) if only else "all"
(OUT / f"chat_{tag}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
n_fail = sum(1 for r in results if r["fails"])
print(f"\nTổng {len(results)} kịch bản, {len(results) - n_fail} PASS, {n_fail} FAIL")
