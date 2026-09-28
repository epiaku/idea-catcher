from pathlib import Path

import pytest
from pydantic import ValidationError

from catcher.modules.llm.profiles import (
    Profile,
    ProfilesConfig,
    UnknownProfile,
    expand_env,
    load_profiles,
    resolve_profile,
)

REPO = Path(__file__).parents[2]


def test_expand_env_uses_value_or_default():
    assert expand_env("m: ${FREELLMAPI_MODEL:-auto}", {}) == "m: auto"
    assert expand_env("m: ${FREELLMAPI_MODEL:-auto}", {"FREELLMAPI_MODEL": "llama"}) == "m: llama"
    assert expand_env("m: ${FREELLMAPI_MODEL:-auto}", {"FREELLMAPI_MODEL": ""}) == "m: auto"


def test_repo_profiles_file_loads_the_three_profiles():
    env = {"OPENAI_MODEL_CLIPPINGS": "gpt-a", "OPENAI_MODEL_YOUTUBE": "gpt-b"}
    cfg = load_profiles(REPO / "profiles.yaml", env=env)
    assert cfg.default == "notes"
    assert cfg.review_profile == "youtube"
    assert cfg.profiles["notes"] == Profile(backend="freellmapi", model="auto")
    assert cfg.profiles["clippings"] == Profile(backend="openai", model="gpt-a")
    assert cfg.profiles["youtube"] == Profile(backend="openai", model="gpt-b")
    assert set(cfg.profiles) == {"notes", "clippings", "youtube", "fake"}


def test_the_file_loads_without_the_openai_variables_but_using_the_profile_fails_clearly():
    cfg = load_profiles(REPO / "profiles.yaml", env={})
    assert resolve_profile(cfg, requested="notes", class_default=None)[0] == "notes"
    with pytest.raises(UnknownProfile, match="clippings.*no model"):
        resolve_profile(cfg, requested="clippings", class_default=None)


def test_old_fields_and_backends_are_rejected(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("default: a\nprofiles:\n  a: {backend: fake, when: evening}\n")
    with pytest.raises(ValidationError):
        load_profiles(path, env={})
    path.write_text("default: a\nprofiles:\n  a: {backend: claude-code, model: sonnet}\n")
    with pytest.raises(ValidationError):
        load_profiles(path, env={})


def test_resolution_order_is_request_then_class_then_global():
    cfg = ProfilesConfig(
        default="a",
        profiles={"a": Profile(backend="fake"), "b": Profile(backend="fake"), "c": Profile(backend="fake")},
    )
    assert resolve_profile(cfg, requested="c", class_default="b")[0] == "c"
    assert resolve_profile(cfg, requested=None, class_default="b")[0] == "b"
    assert resolve_profile(cfg, requested=None, class_default=None)[0] == "a"


def test_unknown_profile_names_the_known_ones():
    cfg = ProfilesConfig(default="a", profiles={"a": Profile(backend="fake")})
    with pytest.raises(UnknownProfile, match="known: a"):
        resolve_profile(cfg, requested="nope", class_default=None)


def test_config_must_define_its_default(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("default: missing\nprofiles:\n  a: {backend: fake}\n")
    with pytest.raises(UnknownProfile):
        load_profiles(path, env={})
