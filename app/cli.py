"""`labagent ...`: preflight, smoke, db, account, job, crawl, reindex, kb, chat, forms, mail, serve, worker, eval."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from app import aio, clock
from app.config import get_loaded
from app.logging import setup_logging


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _services(pool_size: int = 3) -> Any:
    from app.services import build_services

    loaded = get_loaded()
    clock.configure(loaded.config.timezone)
    return loaded, build_services(loaded, pool_size=pool_size)


# ------------------------------------------------------------------ lệnh


async def cmd_preflight(a: argparse.Namespace) -> int:
    from app.preflight import run_preflight

    loaded, svc = _services()
    try:
        rep = await run_preflight(loaded, svc, emails=a.email, skip_llm=a.skip_llm, verbose=a.verbose)
    finally:
        await svc.aclose()
    print(rep.render())
    return 1 if rep.failed else 0


async def cmd_smoke(a: argparse.Namespace) -> int:
    from app.llm.smoke import run_smoke

    loaded = get_loaded()
    clock.configure(loaded.config.timezone)
    checks = await run_smoke(loaded, a.model or None, a.out)
    for c in checks:
        mark = {True: "✓", False: "✗", None: "—"}[c.ok]
        print(f"{mark} {c.target:32} {c.feature:36} {c.detail}")
    print(f"\nĐã ghi {a.out}")
    return 0


async def cmd_db(a: argparse.Namespace) -> int:
    from app.db.migrate import upgrade
    from app.db.session import make_engine

    loaded = get_loaded()
    engine = make_engine(loaded.settings.database_url, pool_size=1)
    try:
        await upgrade(engine, a.revision)
    finally:
        await engine.dispose()
    print(f"Đã nâng DB lên {a.revision}")
    return 0


async def cmd_account(a: argparse.Namespace) -> int:
    from app.auth import accounts
    from app.mail.mailer import OutgoingMail, Recipient
    from app.mail.templates import render

    _, svc = _services()
    try:
        if a.action == "list":
            async with svc.sm() as s:
                for acc in await accounts.list_accounts(s):
                    flags = [
                        f
                        for f, v in (
                            ("disabled", acc.disabled),
                            ("must_change", acc.must_change_password),
                            ("locked", bool(acc.locked_until and acc.locked_until > clock.now())),
                        )
                        if v
                    ]
                    print(f"{acc.email:40} {' '.join(flags)}")
            return 0
        if not a.email:
            print("Cần --email", file=sys.stderr)
            return 2
        pw: str | None = None
        if a.action == "create" and not a.force:
            from app.auth.account_mail import is_lab_email

            if not await is_lab_email(svc, a.email):
                print(
                    "Email không có trong Members hoặc auth.admins: tài khoản sẽ không đăng nhập được. "
                    "Thêm vào Members trước (hoặc dùng --force).",
                    file=sys.stderr,
                )
                return 2
        async with svc.sm() as s, s.begin():
            if a.action == "create":
                acc, pw = await accounts.create_account(s, a.email)
                if a.role_check:
                    snap = await svc.members.snapshot(force=True)
                    from app.auth.roles import resolve_role

                    role, m = resolve_role(acc.email, snap, svc.config.auth.admin_emails())
                    print(f"Role hiện tại theo Members: {role.value}{f' ({m.ho_ten})' if m else ''}")
            elif a.action == "reset":
                pw = await accounts.reset_password(s, a.email)
            elif a.action in ("disable", "enable"):
                await accounts.set_disabled(s, a.email, a.action == "disable")
            elif a.action == "unlock":
                await accounts.unlock(s, a.email)
            elif a.action == "delete":
                await accounts.delete_account(s, a.email)
        if pw is not None:
            if a.no_mail:
                print(f"Mật khẩu tạm của {a.email}: {pw}")
            else:
                subject, body = render(
                    "account_created",
                    lab_name=svc.config.lab.name,
                    email=a.email,
                    temp_password=pw,
                    login_url=a.login_url,
                )
                await svc.mailer.send(OutgoingMail([Recipient(a.email)], subject, body))
                print(f"Đã gửi mật khẩu tạm tới {a.email}")
        await svc.audit.log("cli", "admin", f"account_{a.action}", a.email, True)
        print("OK")
        return 0
    finally:
        await svc.aclose()


async def cmd_job(a: argparse.Namespace) -> int:
    from app.jobs.base import JOBS, load_all, run_job

    load_all()
    if a.action == "list":
        loaded = get_loaded()
        for name in sorted(JOBS):
            print(f"{name:22} {loaded.config.schedule.get(name, '(chạy tay)')}")
        return 0
    params = dict(p.split("=", 1) for p in (a.param or []))
    _, svc = _services()
    try:
        today = date.fromisoformat(a.today) if a.today else None
        res = await run_job(svc, a.name, today=today, dry_run=a.dry_run, params=params)
        previews = res.pop("previews", None) if isinstance(res, dict) else None
        _print(res)
        for p in previews or []:  # dry-run: in nguyên văn mail sẽ gửi
            print("\n" + "=" * 72 + f"\nTo: {p['to']}\nSubject: {p['subject']}\n" + "-" * 72)
            print(p["body"])
    finally:
        await svc.aclose()
    return 0


async def cmd_crawl(a: argparse.Namespace) -> int:
    from app.jobs.base import run_job
    from app.jobs.web_crawl import make_crawler

    _, svc = _services()
    try:
        await svc.store.ensure()
        if a.apply_run:
            crawler, fetcher = make_crawler(svc, a.source)
            try:
                rep = await crawler.apply_run(a.apply_run)
            finally:
                await fetcher.aclose()
            await svc.audit.log(
                "cli", "admin", "web_crawl_apply_run", f"run {a.apply_run}", True, rep.stats()
            )
            _print(rep.stats())
            return 0
        _print(
            await run_job(
                svc,
                "web_crawl",
                dry_run=a.dry_run,
                params={"mode": "full" if a.full else "quick", "force": a.force},
            )
        )
    finally:
        await svc.aclose()
    return 0


async def cmd_reindex(a: argparse.Namespace) -> int:
    """Index lại toàn bộ vào collection theo config (đổi model/số chiều), rồi mới chuyển alias."""
    from sqlalchemy import delete, update

    from app.db.models import IndexDoc, WebPage
    from app.jobs.base import run_job
    from app.rag.indexer import Indexer
    from app.rag.store import QdrantStore

    if not a.all:
        print("Dùng: labagent reindex --all", file=sys.stderr)
        return 2
    loaded, svc = _services()
    s, cfg = loaded.settings, loaded.config
    try:
        target = cfg.qdrant_collection
        live: QdrantStore = svc.store
        await live.ensure(target, set_alias_if_missing=False)
        writer = QdrantStore(
            s.qdrant_url,
            s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else None,
            target,
            target,
            cfg.embedding.dimensions,
        )  # ghi thẳng vào collection mới
        svc.store = writer
        svc.indexer = Indexer(writer, svc.embedder, svc.sm, cfg.index, target)
        async with svc.sm() as ss, ss.begin():
            await ss.execute(delete(IndexDoc))
            await ss.execute(update(WebPage).values(content_hash=None))
        _print({"kb_reconcile": await run_job(svc, "kb_reconcile")})
        _print({"web_crawl": await run_job(svc, "web_crawl", params={"mode": "full", "force": True})})
        await live.switch_alias(target)
        print(f"Alias {cfg.qdrant_alias} → {target}")
    finally:
        await svc.aclose()
    return 0


async def cmd_kb(a: argparse.Namespace) -> int:
    from app.auth.roles import Role

    _, svc = _services()
    try:
        res = await svc.retriever.search(a.query, Role(a.role))
        for h in res.hits:
            print(
                f"[{h.score:.3f}] {h.payload.get('access')} {h.payload.get('doc_type')} {h.payload.get('location')}"
            )
            print("   " + h.text[:300].replace("\n", " "))
        print(f"\n{len(res.hits)} kết quả, rerank={'có' if res.reranked else 'không (RRF)'}")
    finally:
        await svc.aclose()
    return 0


async def cmd_chat(a: argparse.Namespace) -> int:
    from app.agents.loop import AgentRunner
    from app.auth.roles import Role, UserContext, resolve_dev_login

    _, svc = _services()
    try:
        member = None
        role = Role(a.role)
        if a.as_name:
            snap = await svc.members.snapshot()
            role, member = resolve_dev_login(a.as_name, snap)
        ctx = UserContext(
            role=role,
            session_id="cli" + "0" * 29,
            email=member.email if member else None,
            member=member,
            dev_login=bool(member),
        )
        runner = AgentRunner(svc)
        conv = None
        async for ev in runner.run_turn(ctx, a.message, conv):
            if ev["type"] == "text":
                print(ev["text"], end="", flush=True)
            elif ev["type"] == "tool":
                print(f"\n  ⚙ {ev['name']} {ev['status']}", file=sys.stderr)
            elif ev["type"] == "reset":
                print("\n[reset]\n")
            elif ev["type"] == "error":
                print(f"\nLỗi: {ev['message']}")
        print()
    finally:
        await svc.aclose()
    return 0


async def cmd_forms(a: argparse.Namespace) -> int:
    """Schema viết tay (schemas/) → template có placeholder trên Drive → form_versions."""
    from pathlib import Path

    from app.forms.schema import load_schema_dir

    loaded = get_loaded()
    schemas = load_schema_dir(loaded.config.forms.schemas_dir)
    if a.action == "check":
        _print(
            {k: {"version": s.version, "fields": len(s.fields), "roles": s.roles} for k, s in schemas.items()}
        )
        return 0
    if a.action == "render":
        # thử chèn placeholder + render dữ liệu mẫu từ một file mẫu cục bộ, không cần Drive/DB
        from app.forms.placeholder import insert_placeholders
        from app.forms.publish import sample_values, self_test
        from app.forms.render import build_context, render_docx

        s = schemas[a.form]
        tpl, ph = insert_placeholders(Path(a.template).read_bytes(), s)
        fields, atts = sample_values(s)
        out = Path(a.out or f"var/{s.form_id}_sample.docx")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(render_docx(tpl, build_context(s, fields, atts, {})))
        _print({"placed": ph.placed, "missing": ph.missing, "problems": self_test(tpl, s), "out": str(out)})
        return 0 if ph.ok else 1
    if a.action == "generate":
        return await _forms_generate(a, schemas)
    from app.forms.publish import publish

    _, svc = _services()
    try:
        if a.action == "list":
            _print(
                [
                    {
                        "form_id": d.form_id,
                        "version": d.version,
                        "roles": d.schema.roles,
                        "template": d.template_file_id,
                    }
                    for d in await svc.forms.list_active()
                ]
            )
            return 0
        if a.action == "deactivate":
            _print({"deactivated": await svc.forms.deactivate(a.form)})
            return 0
        targets = [schemas[a.form]] if a.form else list(schemas.values())
        failed = 0
        for s in targets:
            rep = await publish(svc, s, dry_run=a.dry_run)
            _print(rep.as_dict())
            failed += 0 if rep.ok else 1
            if rep.ok and not a.dry_run:
                await svc.audit.log("cli", "admin", "form_publish", f"{s.form_id}@{s.version}", True)
        return 1 if failed else 0
    finally:
        await svc.aclose()


async def _forms_generate(a: argparse.Namespace, schemas: dict[str, Any]) -> int:
    """P4: sinh schema từ một file mẫu đơn cục bộ (không ghi Drive/DB). --compare so với schema viết tay."""
    from pathlib import Path

    import yaml

    from app import clock
    from app.forms.schema_gen.compare import compare
    from app.forms.schema_gen.pipeline import generate

    if not a.template:
        print("Cần --template <file.docx>")
        return 2
    src = Path(a.template)
    svc = None
    if not a.no_llm:
        _, svc = _services()
    try:
        ref = schemas.get(a.compare) if a.compare else None
        res = await generate(
            src.read_bytes(),
            src.name,
            llm=svc.llm if svc else None,
            today=clock.today(),
            roles=list(ref.roles) if ref else None,
        )
    finally:
        if svc is not None:
            await svc.aclose()
    out_dir = Path(a.out or f"var/schema_gen/{res.schema.form_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "schema.yaml").write_text(
        yaml.safe_dump(res.schema.dump(), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (out_dir / "blank.docx").write_bytes(res.blank)
    (out_dir / "template.docx").write_bytes(res.template)
    result: dict[str, Any] = {
        "form_id": res.schema.form_id,
        "fields": [f"{f.key} ({f.type}, {f.blank}, {f.filled_by})" for f in res.schema.fields],
        "report": res.report.as_dict(),
        "out": str(out_dir),
    }
    if ref is not None:
        result["compare"] = compare(res.schema, ref).as_dict()
    _print(result)
    return 0


async def cmd_mail(a: argparse.Namespace) -> int:
    from app.mail.mailer import OutgoingMail, Recipient
    from app.mail.templates import render

    _, svc = _services()
    try:
        subject, body = render(
            "test_mail", lab_name=svc.config.lab.name, sent_at=clock.now().strftime("%d/%m/%Y %H:%M")
        )
        sent = await svc.mailer.send(
            OutgoingMail([Recipient(a.to, a.name or "")], subject, body), dry_run=a.dry_run
        )
        _print(
            {"subject": sent.subject, "to": sent.to, "message_id": sent.message_id, "dry_run": sent.dry_run}
        )
        if not a.dry_run:
            await svc.audit.log("cli", "admin", "mail_test", a.to, True)
    finally:
        await svc.aclose()
    return 0


async def cmd_eval(a: argparse.Namespace) -> int:
    if a.action == "rag":
        from tests.eval.run_rag_eval import main as rag_main

        return int(await rag_main(a))
    from tests.eval.run_eval import main as eval_main

    return int(await eval_main(a))


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from app.main import create_app

    config = uvicorn.Config(
        create_app(),
        host=a.host,
        port=a.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_config=None,
        access_log=False,
    )
    aio.run(uvicorn.Server(config).serve())
    return 0


def cmd_worker(_: argparse.Namespace) -> int:
    from app.worker import main

    try:
        aio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labagent", description="Lab Agent Platform")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("preflight", help="Kiểm tra config, Google, Drive, sheet, DB, Qdrant, LLM")
    x.add_argument("--email", action="append", help="email cần thử resolve role (lặp được)")
    x.add_argument("--skip-llm", action="store_true")
    x.add_argument("--verbose", action="store_true", help="in mọi giá trị config kèm nguồn")

    x = sub.add_parser("smoke", help="Smoke test gateway")
    x.add_argument("what", choices=["llm"])
    x.add_argument("--model", action="append")
    x.add_argument("--out", default="docs/gateway-compat.md")

    x = sub.add_parser("db", help="Migration")
    x.add_argument("action", choices=["upgrade"])
    x.add_argument("revision", nargs="?", default="head")

    x = sub.add_parser("account", help="Quản lý tài khoản do admin cấp")
    x.add_argument("action", choices=["create", "reset", "disable", "enable", "unlock", "delete", "list"])
    x.add_argument("--email")
    x.add_argument(
        "--force", action="store_true", help="create: bỏ qua kiểm tra email có trong Members/admins"
    )
    x.add_argument("--no-mail", action="store_true", help="in mật khẩu tạm ra màn hình thay vì gửi mail")
    x.add_argument("--role-check", action="store_true", help="in role hiện tại theo Members")
    x.add_argument("--login-url", default="http://localhost:8000/login")

    x = sub.add_parser("job", help="Job định kỳ")
    x.add_argument("action", choices=["run", "list"])
    x.add_argument("name", nargs="?")
    x.add_argument("--today", help="YYYY-MM-DD (tiêm ngày để chạy lại/kiểm thử)")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument(
        "--param",
        action="append",
        help='k=v. Job gửi mail: only="Họ Tên" (chỉ gửi người này), resend=1 (gửi lại), round=2 (duty_remind đợt 2)',
    )

    x = sub.add_parser("crawl", help="Crawl website lab")
    x.add_argument("--now", action="store_true", help="(mặc định) chạy ngay")
    x.add_argument("--full", action="store_true")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--force", action="store_true", help="bỏ qua chốt an toàn 30%")
    x.add_argument("--apply-run", type=int)
    x.add_argument("--source")

    x = sub.add_parser("reindex", help="Index lại toàn bộ (đổi model embedding)")
    x.add_argument("--all", action="store_true")

    x = sub.add_parser("kb", help="Tra thử KB")
    x.add_argument("action", choices=["search"])
    x.add_argument("query")
    x.add_argument("--role", default="guest", choices=["guest", "member", "thu_ki", "leader", "admin"])

    x = sub.add_parser("chat", help="Chat thử với agent")
    x.add_argument("message")
    x.add_argument("--role", default="guest", choices=["guest", "member", "thu_ki", "leader", "admin"])
    x.add_argument("--as", dest="as_name", help="đăng nhập với tư cách <họ tên> (dev)")

    x = sub.add_parser("forms", help="Form engine: kiểm tra schema, publish template, liệt kê")
    x.add_argument("action", choices=["check", "publish", "list", "render", "deactivate", "generate"])
    x.add_argument("--form", help="form_id (mặc định: mọi schema trong schemas/)")
    x.add_argument(
        "--dry-run",
        action="store_true",
        help="(publish) chỉ chèn placeholder + tự kiểm tra, không ghi Drive/DB",
    )
    x.add_argument("--template", help="(render/generate) file mẫu đơn .docx cục bộ")
    x.add_argument("--out", help="(render) file docx kết quả; (generate) thư mục kết quả")
    x.add_argument("--no-llm", action="store_true", help="(generate) chỉ dùng luật, không gọi LLM")
    x.add_argument("--compare", help="(generate) form_id trong schemas/ để so sánh")

    x = sub.add_parser("mail", help="Gửi mail kiểm tra")
    x.add_argument("action", choices=["test"])
    x.add_argument("--to", required=True)
    x.add_argument("--name")
    x.add_argument("--dry-run", action="store_true")

    x = sub.add_parser("eval", help="Chạy bộ eval hỏi đáp (tốn tiền LLM)")
    x.add_argument("action", choices=["run", "rag"], help="run = eval agent 30 câu; rag = eval RAG cho KB")
    x.add_argument("--limit", type=int)
    x.add_argument("--only")
    x.add_argument("--out", default="var/eval")
    x.add_argument("--retrieval-only", action="store_true", help="(rag) chỉ đo retrieval, không gọi LLM")
    x.add_argument("--concurrency", type=int, default=3, help="(rag) số câu chạy song song")

    x = sub.add_parser("serve", help="Chạy API")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=8000)

    sub.add_parser("worker", help="Chạy worker (outbox + scheduler)")
    return p


ASYNC = {
    "preflight": cmd_preflight,
    "smoke": cmd_smoke,
    "db": cmd_db,
    "account": cmd_account,
    "job": cmd_job,
    "crawl": cmd_crawl,
    "reindex": cmd_reindex,
    "kb": cmd_kb,
    "chat": cmd_chat,
    "mail": cmd_mail,
    "forms": cmd_forms,
    "eval": cmd_eval,
}
SYNC = {"serve": cmd_serve, "worker": cmd_worker}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    a = build_parser().parse_args(argv)
    if a.cmd in SYNC:
        return SYNC[a.cmd](a)
    loaded = get_loaded()
    setup_logging(loaded.settings.log_level, json=False)
    return aio.run(ASYNC[a.cmd](a))


if __name__ == "__main__":
    sys.exit(main())
