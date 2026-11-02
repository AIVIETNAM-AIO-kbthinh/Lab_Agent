"""Gmail client: chỉ gửi mail (scope gmail.send)."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Protocol

from app.google.base import GoogleService


class GmailAPI(Protocol):
    async def send(self, message: EmailMessage) -> str: ...


class GoogleGmail:
    def __init__(self, creds: Any, timeout: float = 60) -> None:
        self._g = GoogleService("gmail", "v1", creds, timeout)

    async def send(self, message: EmailMessage) -> str:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        res = await self._g.call(lambda s: s.users().messages().send(userId="me", body={"raw": raw}))
        return str(res.get("id", ""))
