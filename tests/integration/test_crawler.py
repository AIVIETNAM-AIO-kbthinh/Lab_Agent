"""Crawl isilab.click (fixture): trang mới / đổi / mất / soft-404 / lỗi mạng / chốt an toàn 30%."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import WebPage
from app.rag.crawler import WebCrawler
from tests.conftest import Env
from tests.fakes.web import FakeWeb, fixture

pytestmark = pytest.mark.db
BASE = "http://isilab.click"


def site() -> dict[str, str | int | Exception]:
    return {
        BASE: fixture("home.vi"),
        f"{BASE}/research": fixture("research.vi"),
        f"{BASE}/members": fixture("members.vi"),
        f"{BASE}/projects": fixture("projects.vi"),
        f"{BASE}/news": fixture("news.vi"),
        f"{BASE}/contact": fixture("contact.vi"),
        f"{BASE}/publications": fixture("projects.vi").replace("Featured Projects", "Publications"),
        f"{BASE}/news/new-lab-equipment": fixture("news-detail.vi"),
    }


def crawler(env: Env, web: FakeWeb, tmp_path) -> WebCrawler:  # type: ignore[no-untyped-def]
    src = env.svc.config.web_sources[0]
    src.langs.values = ["vi"]
    src.max_depth = 1
    return WebCrawler(src, BASE + "/", web, env.svc.sm, env.svc.indexer, tmp_path / "snap")


async def pages(env: Env) -> dict[str, WebPage]:
    async with env.svc.sm() as s:
        return {p.url_canonical.split("?")[0]: p for p in (await s.scalars(select(WebPage))).all()}


async def test_full_crawl_then_unchanged_does_not_reindex(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    web = FakeWeb(site())
    c = crawler(env, web, tmp_path)
    r1 = await c.run("full")
    assert r1.status == "APPLIED" and r1.new >= 7 and r1.errors == 0
    assert all(u.startswith(BASE) and "lang=vi" in u for u in web.fetched)
    assert not any("/admin" in u or "/gallery" in u for u in web.fetched)  # exclude
    docs_before, emb_before = env.store.docs(), env.embedder.calls
    assert len(docs_before) == r1.new
    assert list((tmp_path / "snap" / "isilab_site").iterdir())  # có snapshot HTML

    r2 = await c.run("full")
    assert (r2.new, r2.changed, r2.deleted) == (0, 0, 0) and r2.unchanged == r1.new
    assert env.embedder.calls == emb_before  # web không đổi ⇒ không reindex gì
    assert env.store.docs() == docs_before


async def test_changed_page_replaces_chunks(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages_ = site()
    c = crawler(env, FakeWeb(pages_), tmp_path)
    await c.run("full")
    contact_id = c.doc_id(f"{BASE}/contact?lang=vi", "vi")
    old_hashes = {p.content_hash for p in env.store.points.values() if p.doc_id == contact_id}
    pages_[f"{BASE}/contact"] = fixture("contact.vi").replace("8:00 - 17:00", "8:30 - 17:30")
    r = await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    assert r.changed == 1 and r.new == 0
    new = [p for p in env.store.points.values() if p.doc_id == contact_id]
    assert new and all(p.content_hash not in old_hashes for p in new)  # chunk cũ đã bị xoá
    assert any("8:30 - 17:30" in p.payload["text"] for p in new)


async def test_missing_twice_then_delete_and_network_error_keeps(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages_ = site()
    await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    news_doc = crawler(env, FakeWeb(), tmp_path).doc_id(f"{BASE}/news/new-lab-equipment?lang=vi", "vi")
    pages_[f"{BASE}/news/new-lab-equipment"] = 404
    pages_[f"{BASE}/research"] = ConnectionError("timeout")  # lỗi mạng: giữ nguyên
    r1 = await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    assert r1.missing == 1 and r1.deleted == 0 and r1.errors == 1
    p = await pages(env)
    assert p[f"{BASE}/news/new-lab-equipment"].status == "missing" and news_doc in env.store.docs()
    assert p[f"{BASE}/research"].missing_count == 0 and p[f"{BASE}/research"].status == "active"
    r2 = await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    assert r2.deleted == 1
    assert news_doc not in env.store.docs()
    assert (await pages(env))[f"{BASE}/news/new-lab-equipment"].status == "deleted"


async def test_soft_404(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages_ = site()
    pages_[f"{BASE}/news/new-lab-equipment"] = fixture("notfound.vi")  # 200 nhưng là trang lỗi
    r = await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    assert r.status == "APPLIED"
    assert f"{BASE}/news/new-lab-equipment" not in await pages(env)


async def test_safety_block_then_apply(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages_ = site()
    await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    for k in (f"{BASE}/research", f"{BASE}/members", f"{BASE}/projects", f"{BASE}/contact"):
        pages_[k] = str(pages_[k]).replace("</h1>", " (giao diện mới)</h1>")
    emb = env.embedder.calls
    r = await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    assert r.status == "BLOCKED" and r.change_ratio > 0.3
    assert env.embedder.calls == emb  # chưa áp dụng gì
    applied = await crawler(env, FakeWeb(), tmp_path).apply_run(r.run_id)
    assert applied.changed == 4 and env.embedder.calls > emb


async def test_homepage_down_aborts(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages_ = site()
    await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    docs = env.store.docs()
    pages_[BASE] = 503
    r = await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    assert r.status == "ABORTED" and env.store.docs() == docs


async def test_quick_mode_finds_new_items_only(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages_ = site()
    await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    web = FakeWeb(pages_)
    r = await crawler(env, web, tmp_path).run("quick")
    assert r.status == "APPLIED" and r.deleted == 0 and r.missing == 0
    fetched = {u.split("?")[0] for u in web.fetched}
    assert r.unchanged >= 1  # trang đã biết và mới đổi trong 30 ngày được tải lại, không đổi thì không index
    assert f"{BASE}/news" in fetched


async def test_short_real_page_is_not_soft_404(env: Env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages_ = site()
    short = fixture("member-detail.vi").replace(
        "Tiến sĩ Kỹ thuật Hàng không Vũ trụ từ Đại học Ulsan, Hàn Quốc (2018). Hướng nghiên cứu: hệ thống mô hình, "
        "hệ thống điều khiển, UAV, robot.",
        "Sinh viên đam mê AI.",
    )
    pages_[f"{BASE}/members/khoa-nguyen-dang"] = short
    pages_[f"{BASE}/members"] = str(pages_[f"{BASE}/members"])
    await crawler(env, FakeWeb(pages_), tmp_path).run("full")
    assert (await pages(env))[f"{BASE}/members/khoa-nguyen-dang"].status == "active"
