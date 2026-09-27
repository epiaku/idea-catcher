from pathlib import Path

import pytest

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


def test_repo_profiles_file_loads():
    cfg = load_profiles(REPO / "profiles.yaml", env={})
    assert cfg.default == "free-fast"
    assert cfg.review_profile == "claude-sub-evening"
    assert cfg.profiles["free-fast"] == Profile(backend="freellmapi", model="auto", when="now")
    assert cfg.profiles["claude-sub-evening"] == Profile(
        backend="claude-code", model="sonnet", when="evening"
    )


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
