"""Cảnh báo admin qua mail (và Sentry nếu bật). Có khoá chống gửi trùng."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app import clock
from app.jobs.idempotency import Idempotency
from app.logging import get_logger
from app.mail.mailer import OutgoingMail, Recipient
from app.mail.templates import render

if TYPE_CHECKING:
    from app.services import Services

log = get_logger(__name__)


async def alert_admins(
    svc: Services, title: str, body: str, *, dedupe_key: str | None = None, dry_run: bool = False
) -> bool:
    recipients = svc.config.alert_recipients()
    if not recipients:
        log.error("alert_no_recipient", title=title)
        return False
    idem = Idempotency(svc.sm)
    if dedupe_key and not dry_run and not await idem.claim(f"alert:{dedupe_key}", "alert"):
        return False
    subject, text = render(
        "admin_alert",
        lab_name=svc.config.lab.name,
        title=title,
        body=body,
        at=clock.now().strftime("%d/%m/%Y %H:%M"),
    )
    try:
        await svc.mailer.send(
            OutgoingMail([Recipient(e) for e in recipients], subject, text), dry_run=dry_run
        )
    except Exception as e:
        log.error("alert_send_failed", title=title, error=str(e)[:200])
        if dedupe_key and not dry_run:
            await idem.release(f"alert:{dedupe_key}")
        return False
    if dedupe_key and not dry_run:
        await idem.done(f"alert:{dedupe_key}")
    try:
        import sentry_sdk

        sentry_sdk.capture_message(f"[labagent] {title}", level="error")
    except Exception:  # noqa: S110
        pass
    return True
