"""Nền chung cho client Google: chạy request sync trong thread, có khoá (httplib2 không thread-safe)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

import anyio.to_thread
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import metrics
from app.errors import NotFound
from app.google.oauth import authorized_http
from app.google.retry import with_retry

T = TypeVar("T")


class GoogleService:
    def __init__(self, api: str, version: str, creds: Any, timeout: float = 60) -> None:
        self.api = api
        self._lock = threading.Lock()
        self._svc = build(api, version, http=authorized_http(creds, timeout), cache_discovery=False)

    @property
    def svc(self) -> Any:
        return self._svc

    async def call(self, make_request: Callable[[Any], Any]) -> Any:
        """`make_request(svc)` trả về HttpRequest; `.execute()` chạy trong thread + retry."""

        def _run() -> Any:
            with self._lock:
                return make_request(self._svc).execute(num_retries=0)

        try:
            result = await anyio.to_thread.run_sync(lambda: with_retry(_run))
        except HttpError as e:
            metrics.GOOGLE_CALLS.labels(self.api, f"http_{e.resp.status}").inc()
            if int(e.resp.status) == 404:
                raise NotFound(f"{self.api}: 404") from e
            raise
        metrics.GOOGLE_CALLS.labels(self.api, "ok").inc()
        return result

    async def call_raw(self, fn: Callable[[Any], T]) -> T:
        """Cho các thao tác không phải HttpRequest (vd MediaIoBaseDownload)."""

        def _run() -> T:
            with self._lock:
                return fn(self._svc)

        return await anyio.to_thread.run_sync(lambda: with_retry(_run))
