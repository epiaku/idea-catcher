import pytest
from pydantic import ValidationError

from catcher.core.config import PROJECT_ROOT, Settings


def test_settings_read_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("IDEAS_REPO", str(tmp_path / "ib"))
    monkeypatch.setenv("TRANSCRIPT_LANGUAGES", "en, nl")
    settings = Settings()
    assert settings.ideas_repo == tmp_path / "ib"
    assert settings.transcript_language_list == ["en", "nl"]


def test_settings_defaults():
    settings = Settings()
    assert settings.profiles_file == PROJECT_ROOT / "profiles.yaml"
    assert settings.openai_api_key.get_secret_value() == ""
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert not hasattr(settings, "claude_bin")


def test_default_paths_do_not_depend_on_the_folder_the_command_runs_from(monkeypatch, tmp_path):
    monkeypatch.delenv("IDEAS_REPO", raising=False)
    monkeypatch.delenv("DOCS_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.ideas_repo == PROJECT_ROOT.parent / "idea-bucket"
    assert settings.docs_repo == PROJECT_ROOT.parent / "epiaku-docs"
    assert settings.profiles_file.is_file()  # the real profiles.yaml, found from anywhere


def test_the_api_keys_do_not_show_in_a_repr():
    settings = Settings(openai_api_key="sk-secret", freellmapi_api_key="free-secret")
    assert "sk-secret" not in repr(settings) and "free-secret" not in repr(settings)
    assert settings.openai_api_key.get_secret_value() == "sk-secret"


def test_settings_llm_trace_defaults(monkeypatch):
    monkeypatch.delenv("LLM_TRACE", raising=False)
    monkeypatch.delenv("LLM_TRACE_PROMPT", raising=False)
    settings = Settings()
    assert settings.llm_trace is True
    assert settings.llm_trace_prompt is False
    monkeypatch.setenv("LLM_TRACE", "false")
    monkeypatch.setenv("LLM_TRACE_PROMPT", "1")
    settings = Settings()
    assert settings.llm_trace is False and settings.llm_trace_prompt is True


def test_llm_block_s_defaults_to_ten_minutes_and_must_be_positive(monkeypatch):
    monkeypatch.delenv("LLM_BLOCK_S", raising=False)
    assert Settings().llm_block_s == 600
    monkeypatch.setenv("LLM_BLOCK_S", "90")
    assert Settings().llm_block_s == 90
    for bad in ("0", "-5"):
        monkeypatch.setenv("LLM_BLOCK_S", bad)
        with pytest.raises(ValidationError, match="llm_block_s"):
            Settings()


def test_youtube_block_hours_must_be_more_than_0_and_at_most_24(monkeypatch):
    """0 or less would turn the breaker off; more than 24 disagrees with the 24 hour ceiling of the gate."""
    monkeypatch.setenv("YOUTUBE_BLOCK_HOURS", "24")
    assert Settings().youtube_block_hours == 24
    monkeypatch.setenv("YOUTUBE_BLOCK_HOURS", "0.5")
    assert Settings().youtube_block_hours == 0.5
    for bad in ("0", "-1", "25", "nan"):
        monkeypatch.setenv("YOUTUBE_BLOCK_HOURS", bad)
        with pytest.raises(ValidationError, match="youtube_block_hours"):
            Settings()
