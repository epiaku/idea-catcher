from catcher.modules.pipeline.doctypes import (
    AI_CHAT,
    NOTE,
    WEB_CLIP,
    YOUTUBE,
    YOUTUBE_GEMINI,
    canonical_source,
    derive_id,
    detect,
    first_user_turn,
)

GEMINI = "https://gemini.google.com/app/cf81e40b020519ef?is_sa=1&utm_source=sem&gclid=Cj0"
CHAT_BODY = "**You**\n\nsysteme.io sell group of items\n\n---\n\n**Gemini**\n\nYes.\n"
YT_CHAT_BODY = (
    "**You**\n\nSummarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\n"
    "Format Requirements\n\n---\n\n**Gemini**\n\n**Success Is Hard** by *Someone*\n"
)


def test_dictated_note_without_frontmatter():
    assert detect({}, "an idea") is NOTE
    assert derive_id(NOTE, {}, "an idea") is None


def test_template_note_keeps_its_numeric_id():
    fm = {"id": 20260924103015, "type": "note"}
    assert detect(fm, "") is NOTE
    assert derive_id(NOTE, fm, "") == "20260924103015"


def test_gemini_chat_id_comes_from_the_url_without_tracking():
    fm = {"source": GEMINI}
    assert detect(fm, CHAT_BODY) is AI_CHAT
    assert derive_id(AI_CHAT, fm, CHAT_BODY) == "cf81e40b020519ef"
    assert canonical_source(AI_CHAT, fm) == "https://gemini.google.com/app/cf81e40b020519ef"


def test_claude_chat():
    fm = {"source": "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"}
    assert detect(fm, "chat") is AI_CHAT
    assert derive_id(AI_CHAT, fm, "chat") == "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


def test_gemini_chat_that_summarized_a_video():
    fm = {"source": GEMINI}
    assert detect(fm, YT_CHAT_BODY) is YOUTUBE_GEMINI
    assert derive_id(YOUTUBE_GEMINI, fm, YT_CHAT_BODY) == "MBPHU7aaklM-gemini"


def test_explicit_type_overrides_detection():
    assert detect({"source": GEMINI, "type": "ai-chat"}, YT_CHAT_BODY) is AI_CHAT


def test_class_from_a_previous_ingest_is_kept_on_replay():
    fm = {"source": GEMINI, "class": "youtube-gemini", "id": "MBPHU7aaklM-gemini"}
    assert detect(fm, "body without a link") is YOUTUBE_GEMINI
    assert derive_id(YOUTUBE_GEMINI, fm, "body without a link") == "MBPHU7aaklM-gemini"


def test_youtube_clip_with_playlist_parameters():
    fm = {"source": "https://www.youtube.com/watch?v=tcqEUSNCn8I&list=PL4RK&index=10&t=1s"}
    assert detect(fm, "page scrape") is YOUTUBE
    assert derive_id(YOUTUBE, fm, "page scrape") == "tcqEUSNCn8I"
    assert canonical_source(YOUTUBE, fm) == "https://www.youtube.com/watch?v=tcqEUSNCn8I"


def test_youtube_link_without_a_video_is_a_web_clip():
    assert detect({"source": "https://www.youtube.com/@pixegami"}, "x") is WEB_CLIP


def test_empty_or_missing_source_is_a_note():
    assert detect({"source": ""}, "x") is NOTE
    assert detect({}, "a dictated idea") is NOTE
    assert detect({"source": "not a link"}, "x") is NOTE
    assert canonical_source(NOTE, {}) is None


def test_unsafe_explicit_id_is_cleaned():
    assert derive_id(NOTE, {"id": "a/b c"}, "") == "a-b-c"
    assert derive_id(NOTE, {"id": "///"}, "") is None


def test_first_user_turn_stops_at_the_first_answer():
    assert first_user_turn(YT_CHAT_BODY).strip().startswith("Summarize this YouTube video")
    assert "Success Is Hard" not in first_user_turn(YT_CHAT_BODY)
    assert first_user_turn("no markers at all") == "no markers at all"


def test_default_llm_profiles():
    assert NOTE.llm_profile == "notes"
    assert AI_CHAT.llm_profile == "clippings"
    assert YOUTUBE.llm_profile == YOUTUBE_GEMINI.llm_profile == "youtube"


def test_output_folders():
    assert AI_CHAT.out_dir == "hugo/content/en/docs/idea-bucket/clippings"
    assert YOUTUBE_GEMINI.out_dir == YOUTUBE.out_dir == "hugo/content/en/docs/idea-bucket/youtube"

    assert not hasattr(YOUTUBE, "archive_dir")
    assert NOTE.out_dir == "hugo/content/en/docs/idea-bucket/notes"


def test_any_other_page_clipped_with_the_web_clipper_is_a_web_clip():
    fm = {
        "source": "https://example.com/blog/hugo-shortcodes-explained?utm_source=newsletter&page=2#comments"
    }
    assert detect(fm, "The article text") is WEB_CLIP
    assert canonical_source(WEB_CLIP, fm) == "https://example.com/blog/hugo-shortcodes-explained?page=2"


def test_a_web_clip_keeps_the_id_when_the_same_page_is_clipped_again_with_other_tracking():
    first = {"source": "https://www.Example.com/blog/post/?utm_source=a&utm_medium=b#top"}
    again = {"source": "https://example.com/blog/post?fbclid=xyz"}
    other = {"source": "https://example.com/blog/other-post"}
    id_first = derive_id(WEB_CLIP, first, "x")
    assert id_first and len(id_first) == 12 and id_first == derive_id(WEB_CLIP, again, "x")
    assert derive_id(WEB_CLIP, other, "x") != id_first


def test_a_query_that_names_the_page_is_part_of_the_web_clip_id():
    a = derive_id(WEB_CLIP, {"source": "https://example.com/view?id=1"}, "x")
    b = derive_id(WEB_CLIP, {"source": "https://example.com/view?id=2"}, "x")
    assert a != b


def test_a_web_clip_can_be_named_explicitly_and_an_id_in_the_frontmatter_wins():
    assert detect({"type": "web-clip"}, "x") is WEB_CLIP
    assert derive_id(WEB_CLIP, {"source": "https://example.com/a", "id": "my-own-id"}, "x") == "my-own-id"


def test_web_clips_use_the_clippings_profile_and_their_own_folder():
    assert WEB_CLIP.llm_profile == "clippings"
    assert WEB_CLIP.out_dir == "hugo/content/en/docs/idea-bucket/web-clips"
    assert WEB_CLIP.task == "web-clip" and WEB_CLIP.schema_name == "WebClipSummary"
