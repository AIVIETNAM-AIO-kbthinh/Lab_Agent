"""Gửi mail qua Gmail. Ở test_mode: tiền tố [TEST] + dòng đầu "Người nhận dự kiến: <họ tên>"."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr

from app.config import AppConfig
from app.domain.names import valid_email
from app.google.gmail import GmailAPI
from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str = ""


@dataclass
class Attachment:
    filename: str
    data: bytes
    mime: str = "application/octet-stream"


@dataclass
class OutgoingMail:
    to: list[Recipient]
    subject: str
    body: str
    cc: list[Recipient] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class SentMail:
    message_id: str | None
    subject: str
    to: list[str]
    dry_run: bool
    body: str


class Mailer:
    def __init__(self, gmail: GmailAPI | None, config: AppConfig, sender: str | None = None) -> None:
        self.gmail = gmail
        self.config = config
        self.sender = sender or config.email.sender

    def prepare(self, mail: OutgoingMail) -> EmailMessage:
        cfg = self.config
        subject, body = mail.subject, mail.body
        if cfg.test_mode:
            subject = f"{cfg.test.subject_prefix} {subject}".strip()
            if cfg.test.show_intended_recipient:
                names = ", ".join(r.name or r.email for r in mail.to)
                body = f"Người nhận dự kiến: {names}\n\n{body}"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = ", ".join(formataddr((r.name, r.email)) if r.name else r.email for r in mail.to)
        if mail.cc:
            msg["Cc"] = ", ".join(formataddr((r.name, r.email)) if r.name else r.email for r in mail.cc)
        if self.sender:
            msg["From"] = self.sender
        msg.set_content(body)
        for a in mail.attachments:
            maintype, _, subtype = a.mime.partition("/")
            msg.add_attachment(
                a.data, maintype=maintype, subtype=subtype or "octet-stream", filename=a.filename
            )
        return msg

    async def send(self, mail: OutgoingMail, *, dry_run: bool = False) -> SentMail:
        bad = [r.email for r in [*mail.to, *mail.cc] if not valid_email(r.email)]
        if bad or not mail.to:
            raise ValueError(f"Địa chỉ email không hợp lệ: {bad or 'trống'}")
        msg = self.prepare(mail)
        if dry_run or self.gmail is None:
            log.info("mail_dry_run", subject=str(msg["Subject"]), to_count=len(mail.to))
            return SentMail(None, str(msg["Subject"]), [r.email for r in mail.to], True, msg.get_content())
        mid = await self.gmail.send(msg)
        log.info("mail_sent", message_id=mid, to_count=len(mail.to))
        return SentMail(mid, str(msg["Subject"]), [r.email for r in mail.to], False, msg.get_content())
