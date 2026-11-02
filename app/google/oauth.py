"""OAuth của tài khoản Gmail riêng của lab. Refresh token ghi lại file qua file tạm + rename."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import google_auth_httplib2
import httplib2
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from app.errors import ExternalServiceError
from app.logging import get_logger

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.send",
]
log = get_logger(__name__)


class GoogleAuthError(ExternalServiceError):
    """Token chết (invalid_grant) hoặc thiếu: health đỏ + alert admin."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp).replace(path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load_credentials(token_file: str | Path, *, refresh: bool = True) -> Credentials:
    path = Path(token_file)
    if not path.exists():
        raise GoogleAuthError(f"Không tìm thấy token Google: {path} (chạy scripts/get_token.py)")
    creds: Credentials = Credentials.from_authorized_user_file(str(path), SCOPES)  # type: ignore[no-untyped-call]
    if refresh and (not creds.valid or creds.expired):
        refresh_credentials(creds, path)
    return creds


def refresh_credentials(creds: Credentials, token_file: str | Path | None = None) -> None:
    try:
        creds.refresh(google_auth_httplib2.Request(httplib2.Http(timeout=30)))  # type: ignore[no-untyped-call]
    except RefreshError as e:
        log.error("google_refresh_failed", error=str(e)[:200])
        raise GoogleAuthError(f"Refresh token Google lỗi: {e}") from e
    if token_file is not None:
        _atomic_write(Path(token_file), creds.to_json())  # type: ignore[no-untyped-call]


def granted_scopes(token_file: str | Path) -> list[str]:
    data = json.loads(Path(token_file).read_text(encoding="utf-8"))
    scopes = data.get("scopes") or []
    return list(scopes) if isinstance(scopes, list) else str(scopes).split()


def authorized_http(creds: Credentials, timeout: float = 60) -> google_auth_httplib2.AuthorizedHttp:
    return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
