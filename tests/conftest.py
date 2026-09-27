from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from catcher.core.frontmatter import Doc, dump
from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.service import LlmResult, Usage
from catcher.modules.pipeline.doctypes import DOC_TYPES
from catcher.modules.pipeline.staging import StagedNote


@pytest.fixture
def prompt_tags() -> dict[str, list[str]]:
    return {
        "idea_types": ["app-idea", "tech-note"],
        "topics": ["automation", "ai-agents"],
        "projects": ["idea-catcher"],
    }


@pytest.fixture
def fake_profiles() -> ProfilesConfig:
    return ProfilesConfig(default="fake", review_profile="fake", profiles={"fake": Profile(backend="fake")})


@pytest.fixture
def make_note():
    def _make(
        doctype: str = "note",
        *,
        doc_id: str = "a7b2c9",
        body: str = "An idea.\n",
        root: Path | None = None,
        **fm: Any,
    ) -> StagedNote:
        base = {
            "id": doc_id,
            "class": doctype,
            "captured": "2026-09-27",
            "source_file": f"inbox/notes/{doc_id}.md",
            "staged_at": "2026-09-27T18:00:00+00:00",
        }
        doc = Doc({**base, **fm}, body)
        path = (root / "staging" / f"{doc_id}.md") if root else Path("staging") / f"{doc_id}.md"
        if root:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump(doc), encoding="utf-8")
        return StagedNote(doc_id, DOC_TYPES[doctype], doc, path)

    return _make


@pytest.fixture
def make_result():
    def _make(
        output: BaseModel,
        *,
        profile: str = "fake",
        backend: str = "fake",
        model: str = "fake",
        version: str = "note-1",
    ) -> LlmResult:
        return LlmResult(output, profile, backend, model, version, Usage(tokens_in=10, tokens_out=5), 1)

    return _make
