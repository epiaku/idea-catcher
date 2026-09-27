import re

from catcher.core.frontmatter import FrontmatterError, parse
from catcher.modules.pipeline.tags import TagList

ALLOWED_SHORTCODES = frozenset({"youtube-lite", "alert"})
REQUIRED_FIELDS = ("title", "description", "weight", "type", "id")
_SHORTCODE = re.compile(r"\{\{[<%]\s*/?\s*([A-Za-z0-9_.-]+)")


def validate_page(page: str, tags: TagList) -> list[str]:
    if not page.startswith("---\n"):
        return ["page has no frontmatter"]
    try:
        doc = parse(page)
    except FrontmatterError as e:
        return [str(e)]
    fm = doc.fm
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        if fm.get(name) in (None, ""):
            problems.append(f"missing frontmatter field {name!r}")
    if fm.get("type") not in (None, "", "docs"):
        problems.append("frontmatter 'type' must be 'docs'")
    if "weight" in fm and (not isinstance(fm["weight"], int) or isinstance(fm["weight"], bool)):
        problems.append("frontmatter 'weight' must be an integer")

    page_tags = fm.get("tags") or []
    if not isinstance(page_tags, list):
        problems.append("frontmatter 'tags' must be a list")
    else:
        unknown = [str(t) for t in page_tags if t not in tags.allowed]
        if unknown:
            problems.append(f"tags not in the allowed list: {', '.join(unknown)}")
        idea_types = [t for t in page_tags if t in tags.idea_types]
        if len(idea_types) != 1:
            problems.append(f"need exactly one idea-type tag, found {len(idea_types)}")

    if not doc.body.strip():
        problems.append("page body is empty")
    bad = sorted({name for name in _SHORTCODE.findall(doc.body) if name not in ALLOWED_SHORTCODES})
    if bad:
        problems.append(f"unknown shortcodes: {', '.join(bad)}")
    return problems
