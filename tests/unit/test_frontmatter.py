import pytest

from catcher.core.frontmatter import Doc, FrontmatterError, dump, parse

DICTATED = "Create YouTube content walking around and talk about your career\n"

CLIP = """---
source : "https://gemini.google.com/app/cf81e40b020519ef?is_sa=1&utm_source=sem"
author:
published:
created: 2026-09-25
description: "Gemini conversation with 12 messages"
tags:
  - "clippings"
---
**You**

systeme.io sell group of items

---

**Gemini**

Yes, you can.
"""


def test_note_without_frontmatter_is_all_body():
    doc = parse(DICTATED)
    assert doc.fm == {}
    assert doc.body == DICTATED


def test_web_clipper_key_with_space_before_colon():
    doc = parse(CLIP)
    assert doc.fm["source"].startswith("https://gemini.google.com/app/cf81e40b020519ef")
    assert doc.fm["tags"] == ["clippings"]
    assert doc.fm["author"] is None
    assert str(doc.fm["created"]) == "2026-09-25"


def test_body_keeps_chat_turn_separators():
    doc = parse(CLIP)
    assert doc.body.startswith("**You**\n")
    assert "\n---\n\n**Gemini**\n" in doc.body


def test_crlf_and_bom_are_normalized():
    doc = parse("﻿---\r\nid: 20260924103015\r\ntype: note\r\n---\r\nHello\r\n")
    assert doc.fm == {"id": 20260924103015, "type": "note"}
    assert doc.body == "Hello\n"


def test_empty_frontmatter():
    assert parse("---\n---\nbody\n") == Doc({}, "body\n")


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: [unclosed\n---\nbody\n",
        "---\ntitle: no closing line\nbody\n",
        "---\n- a\n- b\n---\nbody\n",
    ],
)
def test_unreadable_frontmatter_raises(text):
    with pytest.raises(FrontmatterError):
        parse(text)


def test_dump_round_trips():
    doc = Doc({"id": "abc123", "title": "Idée: café | test", "tags": ["app-idea"]}, "Body\n\n---\n\nMore\n")
    assert parse(dump(doc)) == doc


def test_dump_quotes_ids_that_look_like_numbers():
    doc = Doc({"id": "1234567e890"}, "x\n")
    assert parse(dump(doc)).fm["id"] == "1234567e890"
