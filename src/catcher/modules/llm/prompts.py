from pathlib import Path
from typing import Any

import jinja2

from catcher.core.frontmatter import load

PROMPTS_DIR = Path(__file__).parent / "prompts"
_env = jinja2.Environment(
    undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True
)


def render_prompt(task: str, variables: dict[str, Any]) -> tuple[str, str]:
    doc = load(PROMPTS_DIR / f"{task}.md")
    return _env.from_string(doc.body).render(**variables), str(doc.fm["version"])
