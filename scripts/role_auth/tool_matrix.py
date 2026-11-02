"""So danh sách tool của từng role với ma trận authentication_plan.md §3. Chạy: python scripts/role_auth/tool_matrix.py"""

import sys

import app.tools.builtin  # noqa: F401  (đăng ký tool vào REGISTRY)
from app.auth.roles import Role
from app.tools.registry import REGISTRY

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

G, M, T, L, A = "guest", "member", "thu_ki", "leader", "admin"
MP = {M, T, L, A}
ALL = {G, M, T, L, A}
EXPECTED = {
    "search_kb": ALL,
    "list_forms": ALL,
    "get_application_status": {G},
    "query_equipment": MP,
    "my_borrows": MP,
    "query_duty": MP,
    "register_duty": MP,
    "query_members": MP,
    "list_pending_applications": {L, A},
    "get_form_template": ALL,
    "start_form": ALL,
    "update_form_fields": ALL,
    "attach_file": ALL,
    "render_preview": ALL,
    "submit_filled_form": ALL,
    "cancel_form": ALL,
    "export_data": MP,
    "export_form_files": MP,
    "return_equipment": MP,
    "extend_borrow": MP,
    "request_member_change": {A},
    "propose_template": {L, A},
    "update_template_draft": {L, A},
    "cancel_template_draft": {L, A},
    "publish_kb_document": {T, L, A},
    "request_data_edit": {L, A},
}

bad = 0
for name in REGISTRY.names():
    d = REGISTRY.get(name)
    assert d is not None
    actual = {r.value for r in d.roles}
    exp = EXPECTED.get(name)
    ok = exp == actual
    bad += not ok
    shown = sorted(exp) if exp else "KHÔNG CÓ TRONG PLAN"
    print(f"{'OK ' if ok else 'BAD'} {name:28} actual={sorted(actual)} expected={shown}")
for n in EXPECTED:
    if n not in REGISTRY.names():
        print("MISSING", n)
        bad += 1
print()
for r in Role:
    tools = REGISTRY.for_role(r)
    print(f"{r.value:7} ({len(tools)} tool):", ", ".join(t.name for t in tools))
print("\nMismatch:", bad)
sys.exit(1 if bad else 0)
