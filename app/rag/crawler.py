"""Crawl website lab (mục 5.3): BFS, hash nội dung chính, chốt an toàn, index trực tiếp vào backend.

Quy tắc:
- Trang mới → index. Hash đổi → thay chunk. Hash không đổi → chỉ cập nhật last_fetched.
- 404/410/soft-404/không còn khám phá thấy (chỉ chế độ full) → missing_count+1; chỉ xoá khi >= ngưỡng.
- Lỗi mạng/5xx/timeout → GIỮ NGUYÊN index, không tăng missing_count.
- Một lần chạy định thay/xoá > max_change_ratio số trang → BLOCKED, chờ admin `labagent crawl --apply-run <id>`.
- Không tải được trang chủ → huỷ cả lần chạy (ABORTED), giữ index cũ.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import clock, metrics
from app.config import WebSourceCfg
from app.db.models import WebCrawlRun, WebPage
from app.logging import get_logger
from app.rag.extract_text import from_html
from app.rag.indexer import Indexer, SourceDoc, doc_id_for

log = get_logger(__name__)
Mode = Literal["quick", "full"]
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|rar|docx?|xlsx?|pptx?|mp4|mp3|css|js|ico)$", re.I)


# ---------------------------------------------------------------- fetch


@dataclass
class FetchResult:
    status: int
    url: str
    text: str
    content_type: str = "text/html"


class FetchError(Exception):
    """Lỗi mạng/5xx/timeout sau khi hết retry: không được coi là trang mất."""


class Fetcher(Protocol):
    async def get(self, url: str) -> FetchResult: ...


class HttpFetcher:
    """1 request / (1/rate) giây, timeout, retry lỗi mạng/5xx. Không lưu cookie (tránh lẫn ngôn ngữ)."""

    def __init__(self, cfg: WebSourceCfg, user_agent: str) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=cfg.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "text/html"},
        )
        self._interval = 1.0 / max(cfg.rate_per_sec, 0.01)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self._interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def get(self, url: str) -> FetchResult:
        last: Exception | None = None
        for attempt in range(self.cfg.retries):
            await self._throttle()
            try:
                r = await self._client.get(url)
                self._client.cookies.clear()
                if r.status_code >= 500:
                    last = FetchError(f"HTTP {r.status_code}")
                else:
                    return FetchResult(r.status_code, str(r.url), r.text, r.headers.get("content-type", ""))
            except httpx.HTTPError as e:
                last = e
            await asyncio.sleep(min(2**attempt, 8))
        raise FetchError(f"{url}: {last}")

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- URL


def canonical(url: str, lang_param: str, lang: str) -> str:
    """Bỏ fragment, chỉ giữ tham số lang, chuẩn hoá path."""
    p = urlparse(url)
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode([(lang_param, lang)])  # lang của lần crawl luôn thắng lang trong link
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


@dataclass
class Rules:
    host: str
    include: list[re.Pattern[str]]
    exclude: list[re.Pattern[str]]

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        if p.scheme not in {"http", "https"} or p.netloc.lower() != self.host:
            return False
        path = p.path or "/"
        if _SKIP_EXT.search(path):
            return False
        if any(x.search(path) for x in self.exclude):
            return False
        return any(x.search(path) for x in self.include)


def extract_links(html: str, page_url: str) -> list[str]:
    import lxml.html

    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return []
    out = []
    for href in root.xpath("//a/@href"):
        h = str(href).strip()
        if not h or h.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append(urljoin(page_url, h))
    return out


# ---------------------------------------------------------------- kết quả 1 trang


@dataclass
class PageData:
    url: str
    lang: str
    depth: int
    status: Literal["ok", "gone", "soft404", "error"]
    title: str = ""
    markdown: str = ""
    doc_date: str | None = None
    content_hash: str | None = None
    error: str | None = None
    links: list[str] = field(default_factory=list)
    html: str | None = None


_MD_HEADING = re.compile(r"^(#{1,6}\s+)(.+?)\s*$", re.MULTILINE)


def apply_heading_aliases(title: str, markdown: str, aliases: dict[str, str]) -> tuple[str, str]:
    """Thêm tên gọi khác vào tiêu đề trang/heading, vd "Contact Us" → "Contact Us (Liên hệ)".

    Website bản tiếng Việt vẫn để tiêu đề mục bằng tiếng Anh ⇒ câu hỏi tiếng Việt ("liên hệ", "lĩnh vực
    nghiên cứu") khớp kém. Chỉ đổi văn bản dùng để index, không đổi URL/nội dung gốc.
    """
    if not aliases:
        return title, markdown
    table = {k.strip().casefold(): v for k, v in aliases.items()}

    def one(h: str) -> str:
        alias = table.get(h.strip().casefold())
        return f"{h} ({alias})" if alias and alias.casefold() not in h.casefold() else h

    return one(title), _MD_HEADING.sub(lambda m: m.group(1) + one(m.group(2)), markdown)


def content_hash(title: str, markdown: str) -> str:
    norm = re.sub(r"\s+", " ", f"{title}\n{markdown}").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@dataclass
class CrawlReport:
    run_id: int
    status: str  # APPLIED | BLOCKED | ABORTED | FAILED | DRY_RUN
    mode: Mode
    fetched: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    deleted: int = 0
    errors: int = 0
    change_ratio: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("detail")
        return d


class WebCrawler:
    def __init__(
        self,
        cfg: WebSourceCfg,
        base_url: str,
        fetcher: Fetcher,
        sm: async_sessionmaker[AsyncSession],
        indexer: Indexer,
        snapshots_dir: Path,
        snapshots_keep: int = 5,
    ) -> None:
        self.cfg = cfg
        self.base = base_url if base_url.endswith("/") else base_url + "/"
        self.fetcher = fetcher
        self.sm = sm
        self.indexer = indexer
        self.snap_dir = snapshots_dir / cfg.id
        self.snap_keep = snapshots_keep
        self.rules = Rules(
            urlparse(self.base).netloc.lower(),
            [re.compile(x) for x in cfg.include],
            [re.compile(x) for x in cfg.exclude],
        )

    def _canon(self, url: str, lang: str) -> str:
        return canonical(url, self.cfg.langs.param, lang)

    def doc_id(self, url: str, lang: str) -> str:
        return doc_id_for("web", self.cfg.id, url, lang)

    # ------------------------------------------------------------ fetch + trích

    async def fetch_page(self, url: str, lang: str, depth: int) -> PageData:
        try:
            r = await self.fetcher.get(url)
        except FetchError as e:
            metrics.CRAWL_PAGES.labels(self.cfg.id, "error").inc()
            return PageData(url, lang, depth, "error", error=str(e)[:300])
        if r.status in (404, 410):
            metrics.CRAWL_PAGES.labels(self.cfg.id, "gone").inc()
            return PageData(url, lang, depth, "gone")
        if r.status >= 400 or "html" not in (r.content_type or "text/html"):
            metrics.CRAWL_PAGES.labels(self.cfg.id, "error").inc()
            return PageData(url, lang, depth, "error", error=f"HTTP {r.status} {r.content_type}")
        ex = from_html(r.text, drop_tags=self.cfg.drop_tags, drop_class_patterns=self.cfg.drop_class_patterns)
        links = extract_links(r.text, r.url)
        head = f"{ex.title}\n{ex.markdown[:600]}".casefold()
        if (
            any(p.casefold() in head for p in self.cfg.soft_404_patterns)
            or len(ex.markdown) < self.cfg.min_content_chars
        ):
            metrics.CRAWL_PAGES.labels(self.cfg.id, "soft404").inc()
            return PageData(url, lang, depth, "soft404", title=ex.title, links=links)
        metrics.CRAWL_PAGES.labels(self.cfg.id, "ok").inc()
        title, markdown = apply_heading_aliases(ex.title, ex.markdown, self.cfg.heading_aliases.get(lang, {}))
        return PageData(
            url,
            lang,
            depth,
            "ok",
            title=title,
            markdown=markdown,
            doc_date=ex.doc_date.isoformat() if ex.doc_date else None,
            content_hash=content_hash(title, markdown),
            links=links,
            html=r.text,
        )

    async def _discover(
        self, mode: Mode, known: dict[tuple[str, str], WebPage]
    ) -> dict[tuple[str, str], PageData]:
        results: dict[tuple[str, str], PageData] = {}
        queue: deque[tuple[str, str, int]] = deque()
        seen: set[tuple[str, str]] = set()
        langs = self.cfg.langs.values

        def push(url: str, lang: str, depth: int) -> None:
            c = self._canon(url, lang)
            if (c, lang) in seen or not self.rules.allowed(c):
                return
            seen.add((c, lang))
            queue.append((c, lang, depth))

        if mode == "full":
            for lang in langs:
                for s in self.cfg.seeds:
                    push(urljoin(self.base, s), lang, 0)
        else:
            recent = clock.now() - timedelta(days=self.cfg.recent_days)
            for lang in langs:
                for s in self.cfg.listing_pages or self.cfg.seeds:
                    push(urljoin(self.base, s), lang, 0)
            for (url, lang), wp in known.items():
                if wp.status == "active" and wp.last_changed and wp.last_changed >= recent:
                    push(url, lang, max(1, self.cfg.max_depth))

        while queue and len(results) < self.cfg.max_pages:
            url, lang, depth = queue.popleft()
            page = await self.fetch_page(url, lang, depth)
            results[(url, lang)] = page
            if page.status != "ok" or depth >= self.cfg.max_depth:
                continue
            for link in page.links:
                c = self._canon(link, lang)
                # quick: chỉ đi sâu thêm để bắt trang MỚI (chưa biết)
                if mode == "quick" and (c, lang) in known:
                    continue
                push(link, lang, depth + 1)
        return results

    # ------------------------------------------------------------ chạy

    async def _known(self) -> dict[tuple[str, str], WebPage]:
        async with self.sm() as s:
            rows = (await s.scalars(select(WebPage).where(WebPage.source_id == self.cfg.id))).all()
            return {(r.url_canonical, r.lang): r for r in rows}

    async def run(self, mode: Mode = "full", *, dry_run: bool = False, force: bool = False) -> CrawlReport:
        async with self.sm() as s, s.begin():
            run = WebCrawlRun(source_id=self.cfg.id, mode=mode, status="RUNNING", stats_json={})
            s.add(run)
            await s.flush()
            run_id = run.id
        report = CrawlReport(run_id, "RUNNING", mode)
        try:
            # trang chủ phải tải được, nếu không huỷ cả lần chạy
            home = await self.fetch_page(
                self._canon(self.base, self.cfg.langs.values[0]), self.cfg.langs.values[0], 0
            )
            if home.status != "ok":
                report.status = "ABORTED"
                report.detail["reason"] = f"Không tải được trang chủ ({home.status}: {home.error or ''})"
                await self._finish(run_id, report, None)
                return report

            known = await self._known()
            results = await self._discover(mode, known)
            plan = self._plan(mode, known, results, report)
            for item in plan["upsert"]:
                page = results.get((item["url"], item["lang"]))
                if page is not None and not dry_run:
                    self.save_snapshot(page)
            active = sum(1 for w in known.values() if w.status == "active")
            affected = report.changed + len(plan["delete"])
            report.change_ratio = round(affected / active, 3) if active else 0.0
            if dry_run:
                report.status = "DRY_RUN"
                await self._finish(run_id, report, plan)
                return report
            if active >= 5 and report.change_ratio > self.cfg.max_change_ratio and not force:
                report.status = "BLOCKED"
                report.detail["reason"] = (
                    f"Lần chạy định thay/xoá {affected}/{active} trang "
                    f"({report.change_ratio:.0%} > {self.cfg.max_change_ratio:.0%})"
                )
                await self._finish(run_id, report, plan)
                return report
            await self._apply(plan, report)
            report.status = "APPLIED"
            await self._finish(run_id, report, {"applied": True, "summary": self._plan_summary(plan)})
            return report
        except Exception as e:
            log.exception("crawl_failed", source=self.cfg.id)
            report.status = "FAILED"
            report.detail["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            await self._finish(run_id, report, None, error=report.detail["error"])
            return report

    def _plan(
        self,
        mode: Mode,
        known: dict[tuple[str, str], WebPage],
        results: dict[tuple[str, str], PageData],
        report: CrawlReport,
    ) -> dict[str, Any]:
        upsert: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        delete: list[dict[str, Any]] = []
        touched: list[dict[str, Any]] = []
        report.fetched = len(results)
        for key, page in results.items():
            wp = known.get(key)
            if page.status == "ok":
                item = {
                    "url": page.url,
                    "lang": page.lang,
                    "title": page.title,
                    "markdown": page.markdown,
                    "doc_date": page.doc_date,
                    "content_hash": page.content_hash,
                    "depth": page.depth,
                }
                if wp is None or wp.status != "active":
                    report.new += 1
                    upsert.append({**item, "kind": "new"})
                elif wp.content_hash != page.content_hash:
                    report.changed += 1
                    upsert.append({**item, "kind": "changed"})
                else:
                    report.unchanged += 1
                    touched.append({"url": page.url, "lang": page.lang})
            elif page.status in ("gone", "soft404"):
                if wp is not None and wp.status != "deleted":
                    self._mark_missing(wp, missing, delete)
            else:
                report.errors += 1
        if mode == "full":
            for key, wp in known.items():
                if key not in results and wp.status != "deleted":
                    self._mark_missing(wp, missing, delete)
        report.missing = len(missing)
        report.deleted = len(delete)
        return {"upsert": upsert, "missing": missing, "delete": delete, "touched": touched}

    def _mark_missing(self, wp: WebPage, missing: list[dict[str, Any]], delete: list[dict[str, Any]]) -> None:
        item = {"url": wp.url_canonical, "lang": wp.lang, "missing_count": wp.missing_count + 1}
        (delete if wp.missing_count + 1 >= self.cfg.delete_after_missing_runs else missing).append(item)

    @staticmethod
    def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "upsert": [{"url": x["url"], "lang": x["lang"], "kind": x["kind"]} for x in plan["upsert"]],
            "missing": plan["missing"],
            "delete": plan["delete"],
            "touched": len(plan["touched"]),
        }

    async def _apply(self, plan: dict[str, Any], report: CrawlReport) -> None:
        now = clock.now()
        for item in plan["upsert"]:
            d = date.fromisoformat(item["doc_date"]) if item.get("doc_date") else None
            outcome = await self.indexer.upsert_doc(
                SourceDoc(
                    doc_id=self.doc_id(item["url"], item["lang"]),
                    source="web",
                    location=item["url"],
                    title=item["title"] or item["url"],
                    access=self.cfg.access,
                    doc_type="web",
                    markdown=item["markdown"],
                    lang=item["lang"],
                    doc_date=d,
                    source_version=item["content_hash"],
                )
            )
            async with self.sm() as s, s.begin():
                wp = await s.scalar(
                    select(WebPage).where(
                        WebPage.source_id == self.cfg.id,
                        WebPage.url_canonical == item["url"],
                        WebPage.lang == item["lang"],
                    )
                )
                if wp is None:
                    wp = WebPage(
                        source_id=self.cfg.id, url_canonical=item["url"], lang=item["lang"], first_seen=now
                    )
                    s.add(wp)
                wp.title, wp.content_hash, wp.doc_date = item["title"][:500], item["content_hash"], d
                wp.status, wp.missing_count, wp.depth = "active", 0, item.get("depth", 0)
                wp.last_fetched, wp.last_changed, wp.chunk_count = now, now, outcome.chunks
        for item in plan["touched"]:
            await self._update_page(item, last_fetched=now, missing_count=0, status="active")
        for item in plan["missing"]:
            await self._update_page(item, missing_count=item["missing_count"], status="missing")
        for item in plan["delete"]:
            await self.indexer.delete_doc(self.doc_id(item["url"], item["lang"]))
            await self._update_page(
                item, missing_count=item["missing_count"], status="deleted", chunk_count=0
            )

    async def _update_page(self, item: dict[str, Any], **values: Any) -> None:
        async with self.sm() as s, s.begin():
            wp = await s.scalar(
                select(WebPage).where(
                    WebPage.source_id == self.cfg.id,
                    WebPage.url_canonical == item["url"],
                    WebPage.lang == item["lang"],
                )
            )
            if wp is not None:
                for k, v in values.items():
                    setattr(wp, k, v)

    async def _finish(
        self, run_id: int, report: CrawlReport, plan: dict[str, Any] | None, error: str | None = None
    ) -> None:
        async with self.sm() as s, s.begin():
            run = await s.get(WebCrawlRun, run_id)
            if run is None:
                return
            run.status = report.status
            run.finished_at = clock.now()
            run.stats_json = {**report.stats(), **{k: v for k, v in report.detail.items() if k != "error"}}
            run.plan_json = plan
            run.error = error

    async def apply_run(self, run_id: int) -> CrawlReport:
        """Admin xác nhận một lần chạy bị BLOCKED: áp dụng đúng kế hoạch đã lưu."""
        async with self.sm() as s:
            run = await s.get(WebCrawlRun, run_id)
            if run is None or run.status != "BLOCKED" or not run.plan_json:
                raise ValueError(f"Run {run_id} không ở trạng thái BLOCKED")
            plan, mode = dict(run.plan_json), run.mode
        report = CrawlReport(run_id, "APPLIED", mode)  # type: ignore[arg-type]
        report.new = sum(1 for x in plan["upsert"] if x["kind"] == "new")
        report.changed = len(plan["upsert"]) - report.new
        report.missing, report.deleted = len(plan["missing"]), len(plan["delete"])
        await self._apply(plan, report)
        await self._finish(run_id, report, {"applied": True, "summary": self._plan_summary(plan)})
        return report

    # ------------------------------------------------------------ snapshot HTML (5 bản gần nhất / trang)

    def save_snapshot(self, page: PageData) -> None:
        if not page.html:
            return
        d = self.snap_dir / self.doc_id(page.url, page.lang)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{clock.now():%Y%m%dT%H%M%S}.html.gz").write_bytes(gzip.compress(page.html.encode("utf-8")))
        files = sorted(d.glob("*.html.gz"))
        for old in files[: -self.snap_keep]:
            old.unlink(missing_ok=True)
