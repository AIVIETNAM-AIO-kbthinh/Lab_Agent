"""Retry có giới hạn cho Google API: 429/5xx/lỗi mạng, backoff mũ có jitter, tối đa 5 lần."""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable
from typing import Any, TypeVar

import httplib2
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.logging import get_logger

T = TypeVar("T")
log = get_logger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


def http_status(e: BaseException) -> int | None:
    if isinstance(e, HttpError):
        return int(e.resp.status)
    return None


def _reason(e: HttpError) -> str:
    try:
        details = e.error_details
        if isinstance(details, list) and details:
            return str(details[0].get("reason", ""))
    except Exception:  # noqa: S110
        pass
    return ""


def is_retryable(e: BaseException) -> bool:
    if isinstance(e, HttpError):
        status = int(e.resp.status)
        return status in RETRY_STATUS or (status == 403 and _reason(e) in RETRY_403_REASONS)
    return isinstance(
        e, (TimeoutError, ConnectionError, socket.timeout, ssl.SSLError, httplib2.HttpLib2Error)
    )


def _before_sleep(rs: Any) -> None:
    exc = rs.outcome.exception() if rs.outcome else None
    log.warning(
        "google_retry",
        attempt=rs.attempt_number,
        status=http_status(exc) if exc else None,
        error=type(exc).__name__ if exc else None,
    )


def with_retry(fn: Callable[[], T], attempts: int = 5) -> T:
    @retry(
        retry=retry_if_exception(is_retryable),
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=0.5, max=20),
        before_sleep=_before_sleep,
        reraise=True,
    )
    def _call() -> T:
        return fn()

    return _call()
