import pytest

from catcher.modules.llm.profiles import Profile, ProfilesConfig


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
