"""FakeWeb: fetcher trả HTML theo URL (dùng fixture isilab.click)."""

from __future__ import annotations

from pathlib import Path

from app.rag.crawler import FetchError, FetchResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


class FakeWeb:
    def __init__(self, pages: dict[str, str | int | Exception] | None = None) -> None:
        # url (không có ?lang) → html | status code | exception
        self.pages: dict[str, str | int | Exception] = dict(pages or {})
        self.fetched: list[str] = []

    async def get(self, url: str) -> FetchResult:
        self.fetched.append(url)
        key = url.split("?")[0].rstrip("/") or url
        page = self.pages.get(key, self.pages.get(key + "/", 404))
        if isinstance(page, Exception):
            raise FetchError(str(page))
        if isinstance(page, int):
            if page >= 500:
                raise FetchError(f"HTTP {page}")
            return FetchResult(page, url, fixture("notfound.vi"))
        return FetchResult(200, url, page)
