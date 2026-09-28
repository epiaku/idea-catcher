from catcher.core.frontmatter import parse
from catcher.modules.llm.schemas import ChatSummary, NoteSummary, WebClipSummary
from catcher.modules.pipeline.render import (
    PageContext,
    fmt_count,
    md_cell,
    page_filename,
    page_name,
    render_page,
    slugify,
)


def test_slugify():
    assert slugify("Idée: Café & Co / 2026!") == "idee-cafe-co-2026"
    assert slugify("🚀🚀") == "untitled"
    long = slugify("word " * 30)
    assert len(long) <= 60 and not long.endswith("-")


def test_page_filename():
    assert (
        page_filename("2026-09-27", "a7b2c9", "Idea Catcher Pipeline")
        == "20260927_a7b2c9_idea-catcher-pipeline.md"
    )


def test_md_cell_escapes_pipes_and_newlines():
    assert md_cell("a | b\nc") == "a \\| b c"


def test_fmt_count():
    assert fmt_count(1400000) == "1,400,000"
    assert fmt_count(None) == "Not available"


def test_note_page(make_note, make_result, snapshot):
    note = make_note("note")
    summary = NoteSummary(
        title="Walk-and-talk  videos",
        description="Film career stories\nwhile walking.",
        body="Record videos while walking and talk about your career.",
        tags=["youtube-idea"],
    )
    ctx = PageContext(note, summary, ["youtube-idea", "content-creation"], make_result(summary))
    page = render_page(ctx)
    doc = parse(page)
    assert doc.fm["title"] == "Walk-and-talk videos"
    assert doc.fm["description"] == "Film career stories while walking."
    assert (doc.fm["type"], doc.fm["weight"], doc.fm["id"], doc.fm["date"]) == (
        "docs",
        100,
        "a7b2c9",
        "2026-09-27",
    )
    assert doc.fm["tags"] == ["youtube-idea", "content-creation"]
    assert doc.fm["llm"] == {
        "profile": "fake",
        "backend": "fake",
        "model": "fake",
        "prompt_version": "note-1",
    }
    assert "source" not in doc.fm
    assert doc.body == "Record videos while walking and talk about your career.\n"
    assert page_name(ctx) == "20260927_a7b2c9_walk-and-talk-videos.md"
    assert page == snapshot


def test_chat_page_sections_and_canonical_source(make_note, make_result, snapshot):
    note = make_note(
        "ai-chat",
        doc_id="cf81e40b020519ef",
        body="**You**\n\nq\n",
        source="https://gemini.google.com/app/cf81e40b020519ef?is_sa=1&utm_source=sem",
    )
    summary = ChatSummary(
        title="Selling course bundles on systeme.io",
        description="Sell a full course before every part exists.",
        summary=["You can sell a bundle before all parts exist."],
        decisions=["Sell the full course now"],
        options=[],
        open_questions=["How are updates delivered?"],
        body="## 🧩 Details\n\nUse one product with drip content.",
        tags=["digital-product-idea"],
    )
    page = render_page(
        PageContext(note, summary, ["digital-product-idea", "online-courses"], make_result(summary))
    )
    doc = parse(page)
    assert doc.fm["source"] == "https://gemini.google.com/app/cf81e40b020519ef"
    assert doc.body.startswith("## 📝 Summary\n\n- You can sell a bundle before all parts exist.\n")
    assert "## ✅ Decisions\n\n- Sell the full course now\n" in doc.body
    assert "## 🔀 Options" not in doc.body
    assert "## ❓ Open Questions\n\n- How are updates delivered?\n" in doc.body
    assert doc.body.endswith("Source: <https://gemini.google.com/app/cf81e40b020519ef>\n")
    assert page == snapshot


def test_extra_frontmatter_is_added_before_llm(make_note, make_result):
    note = make_note("note")
    summary = NoteSummary(title="T", description="D", body="B", tags=[])
    page = render_page(PageContext(note, summary, ["todo"], make_result(summary)), extra_fm={"video_id": "x"})
    keys = list(parse(page).fm)
    assert keys.index("video_id") < keys.index("llm")


def test_web_clip_page_sections_and_clean_source(make_note, make_result):
    note = make_note(
        "web-clip",
        doc_id="a1b2c3d4e5f6",
        body="The article text.\n",
        source="https://example.com/blog/hugo?utm_source=news#intro",
    )
    summary = WebClipSummary(
        title="Hugo shortcodes explained",
        description="How to call a snippet from Markdown.",
        summary=["A shortcode is a snippet."],
        key_points=["Use {{< name >}} in Markdown"],
        ideas_to_use=["Add a card shortcode to the docs"],
        body="## 🧩 Details\n\nMore detail.",
        tags=["hugo"],
    )
    page = render_page(PageContext(note, summary, ["tech-note", "hugo"], make_result(summary)))
    doc = parse(page)
    assert doc.fm["source"] == "https://example.com/blog/hugo"
    assert doc.body.startswith("## 📝 Summary\n\n- A shortcode is a snippet.\n")
    assert "## 🔑 Key Points\n\n- Use {{< name >}} in Markdown\n" in doc.body
    assert "## 💡 Ideas to Use It\n\n- Add a card shortcode to the docs\n" in doc.body
    assert doc.body.endswith("Source: <https://example.com/blog/hugo>\n")


def test_web_clip_sections_without_content_are_left_out(make_note, make_result):
    note = make_note("web-clip", doc_id="a1b2c3d4e5f6", source="https://example.com/x")
    summary = WebClipSummary(
        title="T", description="D", summary=["S"], key_points=[], ideas_to_use=[], body="", tags=[]
    )
    body = parse(render_page(PageContext(note, summary, ["todo"], make_result(summary)))).body
    assert "Key Points" not in body and "Ideas to Use It" not in body
