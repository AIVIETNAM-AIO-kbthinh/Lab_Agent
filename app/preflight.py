"""`labagent preflight`: kiểm tra mọi thứ trước khi chạy thật. Có ✗ ⇒ exit code 1."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app import clock
from app.auth.roles import resolve_role
from app.config import LoadedConfig, dev_login_enabled, lint_env_file
from app.domain.dates import week_of
from app.errors import HeaderMissing
from app.google.oauth import SCOPES, granted_scopes, load_credentials
from app.services import Services
from app.sheets.a1 import quote_tab
from app.sheets.table import norm_header

KEY_SETTINGS = [
    "lab.id",
    "lab.website",
    "test_mode",
    "test.dev_login",
    "drive.root_folder_id",
    "timezone",
    "llm.profiles.member_agent.model",
    "llm.profiles.guest_agent.model",
    "llm.profiles.extract.model",
    "llm.profiles.schema_gen.model",
    "llm.fallback.anthropic",
    "llm.fallback.openai_compat",
    "embedding.model",
    "embedding.dimensions",
    "rerank.model",
    "rerank.enabled",
    "llm.budgets.lab_usd_per_month_hard_stop",
]


@dataclass
class Report:
    lines: list[tuple[str, str, str]] = field(default_factory=list)  # (mark, section, message)

    def ok(self, section: str, msg: str) -> None:
        self.lines.append(("✓", section, msg))

    def warn(self, section: str, msg: str) -> None:
        self.lines.append(("!", section, msg))

    def fail(self, section: str, msg: str) -> None:
        self.lines.append(("✗", section, msg))

    @property
    def failed(self) -> bool:
        return any(m == "✗" for m, _, _ in self.lines)

    def render(self) -> str:
        out, cur = [], None
        for mark, sec, msg in self.lines:
            if sec != cur:
                out.append(f"\n[{sec}]")
                cur = sec
            out.append(f"  {mark} {msg}")
        n_fail = sum(1 for m, _, _ in self.lines if m == "✗")
        n_warn = sum(1 for m, _, _ in self.lines if m == "!")
        out.append(f"\nKết quả: {'LỖI' if n_fail else 'XANH'} — {n_fail} lỗi, {n_warn} cảnh báo")
        return "\n".join(out)


def _get(d: Any, dotted: str) -> Any:
    for p in dotted.split("."):
        d = d.get(p) if isinstance(d, dict) else None
    return d


async def run_preflight(
    loaded: LoadedConfig,
    svc: Services,
    *,
    emails: list[str] | None = None,
    skip_llm: bool = False,
    verbose: bool = False,
) -> Report:
    r = Report()
    s, cfg = loaded.settings, loaded.config

    # ---------------------------------------------------------------- config
    sec = "Config"
    for w in lint_env_file(".env"):
        r.warn(sec, w)
    dump = cfg.model_dump(mode="json")
    keys = sorted(loaded.sources) if verbose else KEY_SETTINGS
    for k in keys:
        r.ok(
            sec,
            f"{k} = {json.dumps(_get(dump, k), ensure_ascii=False)}  ({loaded.sources.get(k, 'default')})",
        )
    if len(s.app_secret_key.get_secret_value()) < 32:
        (r.fail if s.is_production else r.warn)(sec, "APP_SECRET_KEY ngắn hơn 32 ký tự")
    if not s.llm_api_key.get_secret_value():
        r.fail(sec, "LLM_API_KEY trống")
    if not cfg.auth.admins:
        (r.fail if s.is_production else r.warn)(sec, "auth.admins trống: chưa có admin")
    for adm in cfg.auth.admins:
        if not adm.ho_ten or adm.ho_ten.strip().lower() == "admin":
            r.warn(
                sec, f"auth.admins: {adm.email} chưa có họ tên thật (dùng khi mượn thiết bị, ghi AuditLog)"
            )
    if dev_login_enabled(s, cfg):
        r.warn(sec, "dev-login ĐANG BẬT (chỉ dùng khi test)")
    if cfg.test_mode:
        r.warn(sec, "test_mode = true: mọi mail có tiền tố [TEST]; tắt khi chạy thật")

    # ---------------------------------------------------------------- DB
    sec = "Postgres"
    try:
        async with svc.sm() as ss:
            await ss.execute(text("select 1"))
        from app.db.migrate import current_revision, head_revision

        rev, head = await current_revision(svc.sm), head_revision()
        if rev == head:
            r.ok(sec, f"kết nối được, migration ở head ({head})")
        else:
            r.fail(sec, f"migration hiện tại {rev}, head {head}: chạy `labagent db upgrade`")
    except Exception as e:
        r.fail(sec, f"không kết nối được: {type(e).__name__}: {str(e)[:150]}")

    # ---------------------------------------------------------------- Qdrant
    sec = "Qdrant"
    try:
        store: Any = svc.store
        await store.health()
        target = await store.alias_target()
        if target is None:
            r.warn(
                sec, f"alias {cfg.qdrant_alias} chưa có (worker sẽ tạo {cfg.qdrant_collection} khi khởi động)"
            )
        elif target != cfg.qdrant_collection:
            r.warn(
                sec,
                f"alias trỏ tới {target} nhưng config muốn {cfg.qdrant_collection}: chạy `labagent reindex --all`",
            )
        else:
            r.ok(sec, f"alias {cfg.qdrant_alias} → {target}, {await store.count()} chunk")
    except Exception as e:
        r.fail(sec, f"không kết nối được: {type(e).__name__}: {str(e)[:150]}")

    # ---------------------------------------------------------------- Google
    sec = "Google OAuth"
    google_ok = False
    tok = Path(s.google_token_file)
    if not Path(s.google_client_secrets_file).exists():
        r.warn(sec, f"không thấy {s.google_client_secrets_file} (chỉ cần khi sinh lại token)")
    if not tok.exists():
        r.fail(sec, f"không thấy token {tok} (chạy scripts/get_token.py rồi chuyển vào secrets/)")
    else:
        try:
            granted = set(granted_scopes(tok))
            missing = [x for x in SCOPES if x not in granted]
            if missing:
                r.fail(sec, f"token thiếu scope: {', '.join(missing)}")
            else:
                r.ok(sec, "đủ 4 scope Drive / Sheets / Docs / Gmail.send")
            creds = load_credentials(tok)
            if not creds.refresh_token:
                r.fail(sec, "token không có refresh_token (sinh lại với access_type=offline, prompt=consent)")
            email = await svc.google.drive.about_email()
            r.ok(sec, f"refresh OK, tài khoản: {email}")
            r.warn(
                sec,
                "nhớ kiểm tra OAuth consent screen ở trạng thái 'In production' (Testing ⇒ token hết hạn 7 ngày)",
            )
            google_ok = True
        except Exception as e:
            r.fail(sec, f"{type(e).__name__}: {str(e)[:200]}")

    if google_ok:
        await _check_drive(r, svc)

    # ---------------------------------------------------------------- vai trò thử
    if google_ok and emails:
        sec = "Vai trò"
        try:
            snap = await svc.members.snapshot(force=True)
            for addr in emails:
                role, m = resolve_role(addr, snap, cfg.auth.admin_emails())
                r.ok(sec, f"{addr} → {role.value}{f' ({m.ho_ten})' if m else ''}")
        except Exception as e:
            r.fail(sec, f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- form engine
    await _check_forms(r, svc)

    # ---------------------------------------------------------------- LLM
    if not skip_llm:
        await _check_llm(r, svc)
    return r


async def _check_drive(r: Report, svc: Services) -> None:
    cfg = svc.config
    sec = "Drive"
    paths = {
        "members": cfg.sheets.members.path,
        "equipment": cfg.sheets.equipment.path,
        "duty": cfg.sheets.duty.path,
        "duty_template": cfg.sheets.duty.template_path,
        "form_submissions": cfg.sheets.form_submissions.path,
        "audit_log": cfg.sheets.audit_log.path,
        "kb_public": cfg.index.kb_public_path,
        "kb_internal": cfg.index.kb_internal_path,
    }
    found: dict[str, bool] = {}
    for name, p in paths.items():
        try:
            f = await svc.paths.resolve(p, refresh=True)
            want_sheet = name not in ("kb_public", "kb_internal")
            if want_sheet and not f.mime_type.endswith("spreadsheet"):
                r.fail(sec, f"{p}: không phải Google Sheet ({f.mime_type}) — bật chuyển đổi xlsx khi upload")
                found[name] = False
            else:
                r.ok(sec, f"{p} → {f.id}")
                found[name] = True
        except Exception as e:
            (r.warn if name.startswith("kb_") else r.fail)(sec, f"{p}: {type(e).__name__}: {str(e)[:150]}")
            found[name] = False

    sec = "Tiêu đề cột"
    if found.get("members"):
        try:
            t = await svc.members.table()
            r.ok(sec, f"Members (tab '{t.tab}', dòng {cfg.sheets.members.header_row}): đủ cột")
            for col in t.missing_optional:
                r.warn(sec, f"Members thiếu cột tuỳ chọn '{col}'")
            snap = await svc.members.snapshot(force=True)
            dups = snap.duplicate_names()
            (r.fail if dups else r.ok)(
                sec,
                f"Members: {len(snap.active)} người đang hoạt động"
                + (f", TRÙNG TÊN: {', '.join(dups)}" if dups else ", không trùng tên"),
            )
            leaders = [m for m in snap.active if m.role == "leader"]
            if len(leaders) != 1:
                r.fail(sec, f"Members phải có đúng 1 Leader (hiện có {len(leaders)})")
            if not snap.secretaries():
                r.warn(sec, "Members chưa có thư ký (Secretary)")
            for name, raw in snap.unknown_roles:
                r.warn(sec, f"Members: '{name}' có Role lạ '{raw}' (coi là Member)")
            no_email = [m.ho_ten for m in snap.active if not m.email]
            if no_email:
                r.warn(sec, f"Members thiếu email: {', '.join(no_email)}")
        except HeaderMissing as e:
            r.fail(sec, str(e))
        except Exception as e:
            r.fail(sec, f"Members: {type(e).__name__}: {e}")
    if found.get("equipment"):
        for label, loader in (
            ("Thietbi", svc.equipment.devices_table),
            ("LichSuMuon", svc.equipment.borrows_table),
        ):
            try:
                t = await loader()
                r.ok(sec, f"{label}: đủ cột bắt buộc ({len(t.rows())} dòng)")
                for col in t.missing_optional:
                    r.warn(sec, f"{label} thiếu cột '{col}' (cột mới theo TK §3.2, cần thêm)")
            except Exception as e:
                r.fail(sec, f"{label}: {e}")
    if found.get("duty"):
        try:
            start, end = week_of(clock.today())
            tab = await svc.duty.load_week(start, end)
            if tab is None:
                r.warn(sec, f"DutySchedule chưa có tab tuần này ({svc.duty.name_for(start, end)})")
            else:
                r.ok(
                    sec,
                    f"DutySchedule '{tab.title}': {len(tab.slots)} buổi, {len(tab.rows)} thành viên, "
                    f"cột số buổi {'có' if tab.count_col else 'KHÔNG thấy'}",
                )
        except Exception as e:
            r.fail(sec, f"DutySchedule: {e}")
    if found.get("duty_template"):
        try:
            from app.domain.duty import parse_duty_grid

            sid = await svc.paths.file_id(cfg.sheets.duty.template_path)
            tabs = await svc.google.sheets.get_tabs(sid)
            tpl = next(
                (t for t in tabs if t.title == cfg.sheets.duty.template_tab), tabs[0] if tabs else None
            )
            if tpl is None:
                r.fail(sec, "LichTrucTuan không có tab")
            else:
                vals = await svc.google.sheets.get_values(sid, f"{quote_tab(tpl.title)}!A1:AZ300")
                tab = parse_duty_grid(vals, cfg.sheets.duty, tpl.title, tpl.sheet_id)
                r.ok(
                    sec,
                    f"Template lịch trực '{tpl.title}': {len(tab.slots)} buổi, {len(tab.numbered_rows)} dòng đánh số",
                )
        except Exception as e:
            r.fail(sec, f"Template lịch trực: {e}")
    if found.get("audit_log"):
        try:
            sid = await svc.paths.file_id(cfg.sheets.audit_log.path)
            tabs = await svc.google.sheets.get_tabs(sid)
            audit_tab = cfg.sheets.audit_log.tab or tabs[0].title
            head = (await svc.google.sheets.get_values(sid, f"{quote_tab(audit_tab)}!A1:G1") or [[]])[0]
            from app.domain.audit import HEADER

            if [norm_header(h) for h in head[:7]] == HEADER:
                r.ok(sec, "AuditLog: tiêu đề đúng")
            else:
                r.warn(sec, f"AuditLog: tiêu đề dòng 1 là {head}, mong đợi {HEADER}")
        except Exception as e:
            r.fail(sec, f"AuditLog: {e}")


async def _check_llm(r: Report, svc: Services) -> None:
    from app.llm.types import UsageContext, user_text

    sec = "LLM / embedding / rerank"
    for name, prof in svc.config.llm.profiles.items():
        if name not in ("member_agent", "guest_agent"):
            continue
        try:
            resp = await svc.llm.complete(
                name, "Trả lời đúng một từ.", [], [user_text("Nói 'ok'.")], UsageContext(profile="preflight")
            )
            r.ok(sec, f"{name} ({resp.model}{', FALLBACK' if resp.degraded else ''}): {resp.latency_ms} ms")
            if resp.degraded:
                r.warn(sec, f"{name}: model chính {prof.model} lỗi, đang dùng fallback")
        except Exception as e:
            r.fail(sec, f"{name} ({prof.model}): {type(e).__name__}: {str(e)[:150]}")
    try:
        v = await svc.embedder.embed(["kiểm tra"])
        dims = len(v[0])
        (r.ok if dims == svc.config.embedding.dimensions else r.fail)(
            sec, f"embedding {svc.config.embedding.model}: {dims} chiều"
        )
    except Exception as e:
        r.fail(sec, f"embedding: {type(e).__name__}: {str(e)[:150]}")
    if svc.reranker is not None:
        res = await svc.reranker.rerank("máy chiếu", ["máy chiếu Epson", "bàn phím"], 2)
        (r.ok if res else r.warn)(
            sec, f"rerank {svc.config.rerank.model}: {'OK' if res else 'lỗi (sẽ dùng RRF)'}"
        )


async def _check_forms(r: Report, svc: Services) -> None:
    from app.forms.render import find_soffice
    from app.forms.schema import load_schema_dir

    sec = "Form engine"
    try:
        schemas = load_schema_dir(svc.config.forms.schemas_dir)
        r.ok(sec, f"schema hợp lệ: {', '.join(sorted(schemas)) or '(không có)'}")
    except Exception as e:
        r.fail(sec, f"schemas/ lỗi: {e}")
        schemas = {}
    soffice = find_soffice(svc.config.render.soffice_path)
    if soffice:
        r.ok(sec, f"LibreOffice: {soffice}")
    else:
        (r.fail if svc.settings.is_production else r.warn)(
            sec,
            "Không thấy LibreOffice: bản xem trước sẽ là DOCX thay vì PDF (cài LibreOffice hoặc đặt render.soffice_path)",
        )
    try:
        active = {d.form_id: d for d in await svc.forms.list_active()}
    except Exception as e:
        r.fail(sec, f"Không đọc được form_versions: {e}")
        return
    for fid, sch in schemas.items():
        d = active.get(fid)
        if d is None:
            r.warn(sec, f"{fid}: chưa publish (chạy `labagent forms publish --form {fid}`)")
        elif d.version != sch.version:
            r.warn(sec, f"{fid}: đang dùng v{d.version}, schemas/ có v{sch.version} (publish lại)")
        elif not d.template_file_id:
            r.fail(sec, f"{fid}: active nhưng thiếu template")
        else:
            r.ok(sec, f"{fid}: active v{d.version}")
