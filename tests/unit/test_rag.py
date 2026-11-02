import io
from datetime import date

import pytest
from docx import Document

from app.rag.chunker import chunk_markdown, count_tokens, heading_access
from app.rag.crawler import Rules, canonical, content_hash, extract_links
from app.rag.drive_source import classify_path, doc_type_for
from app.rag.extract_text import from_docx, from_html, minutes_date
from app.rag.indexer import chunk_access
from app.rag.sparse import doc_vector, query_vector
from tests.conftest import make_loaded
from tests.fakes.web import fixture


@pytest.fixture
def web_cfg():  # type: ignore[no-untyped-def]
    return make_loaded().config.web_sources[0]


def _x(name: str, cfg):  # type: ignore[no-untyped-def]
    return from_html(fixture(name), drop_tags=cfg.drop_tags, drop_class_patterns=cfg.drop_class_patterns)


def test_html_extract_keeps_headings_drops_chrome(web_cfg) -> None:  # type: ignore[no-untyped-def]
    ex = _x("members.vi", web_cfg)
    assert ex.title == "Meet the Team" and ex.lang == "vi"
    assert "### Dr. Khoa Nguyen Dang" in ex.markdown  # tên trong h3 được giữ (trafilatura bỏ mất)
    assert "Quick Links" not in ex.markdown and "Send Message" not in ex.markdown  # footer/form bị bỏ
    contact = _x("contact.vi", web_cfg)
    assert "Phan Tây Nhạc" in contact.markdown and "Your Name" not in contact.markdown
    home = _x("home.vi", web_cfg)
    assert "\n" not in home.title


def test_news_date_and_soft404(web_cfg) -> None:  # type: ignore[no-untyped-def]
    assert _x("news-detail.vi", web_cfg).doc_date == date(2026, 7, 23)
    nf = _x("notfound.vi", web_cfg)
    assert "page not found" in nf.markdown.lower()


def test_extract_is_deterministic(web_cfg) -> None:  # type: ignore[no-untyped-def]
    a, b = _x("projects.vi", web_cfg), _x("projects.vi", web_cfg)
    assert content_hash(a.title, a.markdown) == content_hash(b.title, b.markdown)


def test_chunker_sizes_overlap_prefix() -> None:
    md = "# Nội quy\n\n" + "\n\n".join(
        f"## Điều {i}\n\n" + ("Thành viên phải giữ gìn thiết bị. " * 40) for i in range(1, 6)
    )
    chunks = chunk_markdown("Nội quy lab", md, chunk_tokens=300, overlap=0.12)
    assert len(chunks) >= 5
    assert all(c.tokens <= 300 * 1.3 for c in chunks)
    assert all(c.text.startswith("Nội quy lab") for c in chunks)
    assert any("Điều 3" in c.heading for c in chunks)


def test_chunker_merges_small_sections() -> None:
    md = "# Team\n\n" + "\n\n".join(f"### Người {i}\n\nGiới thiệu ngắn." for i in range(30))
    chunks = chunk_markdown("Team", md, chunk_tokens=700)
    assert len(chunks) == 1 and count_tokens(chunks[0].text) < 700


def test_chunker_section_access_labels() -> None:
    md = (
        "# Nội quy\n\nBảng phân loại các chương.\n\n"
        "## Chương I. Quy định chung [Công khai]\n\nĐiều 1. Phạm vi.\n\n"
        "## Chương III. Giờ làm việc [Nội bộ]\n\nĐiều 10. Mở cửa 8h00 – 21h00.\n\n"
        "### Điều 11. Ra vào\n\nMỗi thành viên có thẻ từ.\n\n"
        "## Chương VII. An toàn [Công khai]\n\nĐiều 24. Không sạc pin qua đêm."
    )
    chunks = chunk_markdown("Nội quy", md, chunk_tokens=700)
    by_access = {c.access: c.text for c in chunks}
    # không gộp nội dung khác nhãn vào một chunk, kể cả phần overlap
    assert not any("21h00" in c.text for c in chunks if c.access != "member")
    assert "thẻ từ" in by_access["member"]  # mục con kế thừa nhãn của chương
    assert [c.access for c in chunks] == [None, "public", "member", "public"]
    assert heading_access(["Doc", "Chapter 2 [Internal]"]) == "member"
    assert heading_access(["Doc", "Chương I"]) is None
    # nhãn chỉ hạ quyền, không mở tài liệu nội bộ
    assert chunk_access("public", "member") == "member"
    assert chunk_access("member", "public") == "member"
    assert chunk_access("public", None) == "public"


def test_sparse_vietnamese_bigrams_and_unaccented() -> None:
    di, dv = doc_vector("Lab có 2 máy chiếu Epson")
    qi, _ = query_vector("may chieu")  # gõ không dấu
    assert set(qi) & set(di)
    q2, _ = query_vector("máy chiếu")
    assert len(set(q2) & set(di)) >= 3  # máy, chiếu, máy_chiếu
    assert all(v > 0 for v in dv) and di == sorted(di)


def test_docx_extract_and_minutes_date() -> None:
    doc = Document()
    doc.add_paragraph("Hà Nội, ngày 12 tháng 10 năm 2026")
    doc.add_heading("BIÊN BẢN HỌP", level=1)
    doc.add_heading("1. Nội dung", level=2)
    doc.add_paragraph("Thảo luận dự án UAV.")
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Việc", "Người phụ trách"
    t.cell(1, 0).text, t.cell(1, 1).text = "Viết báo cáo", "Lê Văn An"
    buf = io.BytesIO()
    doc.save(buf)
    ex = from_docx(buf.getvalue(), "2026-10-12_BienBanHop")
    assert "# BIÊN BẢN HỌP" in ex.markdown and "## 1. Nội dung" in ex.markdown
    assert "Viết báo cáo | Lê Văn An" in ex.markdown
    assert ex.doc_date == date(2026, 10, 12) == minutes_date(ex.markdown)


def test_drive_access_classification() -> None:
    cfg = make_loaded().config.index
    assert classify_path("kb/public/NoiQuy", cfg) == "public"
    assert classify_path("kb/internal/meeting-minutes/2026-10-12_BienBanHop", cfg) == "member"
    assert classify_path("kb/internal/_nhap/x", cfg) is None
    assert classify_path("data/Members", cfg) is None  # data/ không bao giờ index
    assert classify_path("_archive/kb/public/x", cfg) is None
    assert doc_type_for("kb/internal/meeting-minutes/x", "x") == "minutes"
    assert doc_type_for("kb/public/Nội quy lab", "Nội quy lab") == "rule"


def test_url_canonical_and_rules(web_cfg) -> None:  # type: ignore[no-untyped-def]
    assert (
        canonical("http://ISILAB.click/news/?lang=en&utm=1#top", "lang", "vi")
        == "http://isilab.click/news?lang=vi"
    )
    assert canonical("http://isilab.click", "lang", "en") == "http://isilab.click/?lang=en"
    import re

    rules = Rules(
        "isilab.click", [re.compile(x) for x in web_cfg.include], [re.compile(x) for x in web_cfg.exclude]
    )
    assert rules.allowed("http://isilab.click/news/new-lab-equipment?lang=vi")
    assert not rules.allowed("http://isilab.click/admin/login?lang=vi")
    assert not rules.allowed("http://isilab.click/gallery?lang=vi")
    assert not rules.allowed("http://other.com/news")
    assert not rules.allowed("http://isilab.click/public/css/site.css")
    links = extract_links(fixture("home.vi"), "http://isilab.click/?lang=vi")
    assert "http://isilab.click/news/new-lab-equipment" in links


def test_member_detail_bio_in_div_without_nav(web_cfg) -> None:  # type: ignore[no-untyped-def]
    ex = _x("member-detail.vi", web_cfg)
    assert ex.title == "Dr. Khoa Nguyen Dang"
    assert "Đại học Ulsan" in ex.markdown  # tiểu sử nằm trong <div>, không phải <p>
    assert "Publications" not in ex.markdown and "Quay lại" not in ex.markdown  # không lẫn menu/nút


def test_heading_aliases_for_untranslated_titles() -> None:
    from app.rag.crawler import apply_heading_aliases

    title, md = apply_heading_aliases(
        "Contact Us",
        "# Contact Us\n\n#### Địa chỉ\n\n1 Phan Tây Nhạc\n\n## research areas\n\nSLAM",
        {"Contact Us": "Liên hệ", "Research Areas": "Lĩnh vực nghiên cứu"},
    )
    assert title == "Contact Us (Liên hệ)"
    assert "# Contact Us (Liên hệ)" in md and "## research areas (Lĩnh vực nghiên cứu)" in md
    assert "#### Địa chỉ\n" in md and "1 Phan Tây Nhạc" in md  # heading khác và nội dung giữ nguyên
    assert apply_heading_aliases("Liên hệ", "x", {"Liên hệ": "Liên hệ"}) == ("Liên hệ", "x")
