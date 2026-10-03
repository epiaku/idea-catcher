"""The approved pages of the frozen real run, and the comparison every whole-pipeline test makes with them.

A page is matched by the name it was captured under (`original_filename`) and must sit in the same section
(notes/, youtube/ or web-clips/) with the same frontmatter and body, except the keys that differ on every
run by design, and only those:

- source_file: the calculated name, `YYYYMMDD-<random 6 hex>-<title>`, on every page;
- id: a note's id is drawn again on every scan (a clip's id comes from its source, so it is compared);
- date: a note has no capture date, so it gets the day of the run (a clip's date is its capture date).
"""

from pathlib import Path

from catcher.core.frontmatter import load
from catcher.core.testdata import DEFAULT_SOURCE
from catcher.modules.pipeline.doctypes import DESTINATIONS

EXPECTED = DEFAULT_SOURCE / "expected"
PAGES = Path("hugo/content/en/docs/idea-bucket")

VOLATILE = frozenset({"source_file"})
VOLATILE_IN_NOTES = frozenset({"id", "date"})


def published_pages(folder: Path) -> dict[str, tuple[Path, dict, str]]:
    """Every page below `folder`, by the name it was captured under (`original_filename`)."""
    pages: dict[str, tuple[Path, dict, str]] = {}
    for path in sorted(folder.rglob("*.md")):
        if path.name == "_index.md":
            continue
        doc = load(path)
        name = doc.fm["original_filename"]
        assert name not in pages, f"two pages for {name}: {pages[name][0]} and {path}"
        pages[name] = (path.relative_to(folder), doc.fm, doc.body)
    return pages


def stable(fm: dict, section: Path) -> dict:
    volatile = VOLATILE | VOLATILE_IN_NOTES if section == Path("notes") else VOLATILE
    return {k: v for k, v in fm.items() if k not in volatile}


def compare_pages(published_dir: Path, expected_dir: Path) -> int:
    """Assert that the pages below `published_dir` equal those below `expected_dir`; return how many."""
    expected, actual = published_pages(expected_dir), published_pages(published_dir)
    assert sorted(actual) == sorted(expected)
    for name, (rel, fm, body) in actual.items():
        want_rel, want_fm, want_body = expected[name]
        assert rel.parent == want_rel.parent, name  # the same section: notes/, youtube/ or web-clips/
        assert rel.parent.as_posix() in DESTINATIONS, name
        assert stable(fm, rel.parent) == stable(want_fm, want_rel.parent), name
        assert body == want_body, name
    return len(actual)
