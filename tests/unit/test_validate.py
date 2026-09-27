from catcher.core.frontmatter import Doc, dump
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.pipeline.validate import validate_page

TAGS = load_tags()
GOOD_FM = {
    "title": "T",
    "description": "D",
    "date": "2026-09-27",
    "weight": 100,
    "type": "docs",
    "id": "a7b2c9",
    "tags": ["app-idea", "obsidian"],
}


def page(fm: dict | None = None, body: str = "Body text.\n") -> str:
    return dump(Doc(GOOD_FM if fm is None else fm, body))


def test_good_page_has_no_problems():
    assert validate_page(page(), TAGS) == []


def test_known_shortcodes_are_allowed():
    body = '{{< youtube-lite AeV5F0ppaGw `Title` >}}\n{{% alert title="x" %}}\nhi\n{{% /alert %}}\n'
    assert validate_page(page(body=body), TAGS) == []


def test_unknown_shortcode_is_rejected():
    problems = validate_page(page(body="See {{< figure src=x >}} here.\n"), TAGS)
    assert problems == ["unknown shortcodes: figure"]


def test_missing_fields_and_wrong_types():
    fm = {**GOOD_FM, "title": "", "type": "blog", "weight": "100"}
    problems = validate_page(page(fm), TAGS)
    assert "missing frontmatter field 'title'" in problems
    assert "frontmatter 'type' must be 'docs'" in problems
    assert "frontmatter 'weight' must be an integer" in problems


def test_tag_rules():
    assert "tags not in the allowed list: apps" in validate_page(
        page({**GOOD_FM, "tags": ["app-idea", "apps"]}), TAGS
    )
    assert "need exactly one idea-type tag, found 0" in validate_page(
        page({**GOOD_FM, "tags": ["obsidian"]}), TAGS
    )
    assert "need exactly one idea-type tag, found 2" in validate_page(
        page({**GOOD_FM, "tags": ["app-idea", "todo"]}), TAGS
    )


def test_empty_body():
    assert "page body is empty" in validate_page(page(body="\n\n"), TAGS)


def test_broken_frontmatter():
    assert validate_page("---\ntitle: [x\n---\nbody\n", TAGS)[0].startswith("invalid YAML")


def test_page_without_frontmatter():
    assert "page has no frontmatter" in validate_page("just text\n", TAGS)
