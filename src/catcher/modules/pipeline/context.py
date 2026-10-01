import re
from pathlib import Path

CONTEXT_FILE = Path(__file__).parent / "epiaku-context.md"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def load_context(path: Path = CONTEXT_FILE) -> str:
    """Who Epiaku is, in your own words, for the Channel Application part of YouTube summaries.

    The text of the file without its HTML comments. A missing or empty file is an empty string, so the
    prompt then falls back to its one-line description.
    """
    if not path.is_file():
        return ""
    return _COMMENT.sub("", path.read_text(encoding="utf-8")).strip()
