from pathlib import Path

from catcher.core.config import Settings


def test_settings_read_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("IDEAS_REPO", str(tmp_path / "ib"))
    monkeypatch.setenv("TRANSCRIPT_LANGUAGES", "en, nl")
    settings = Settings()
    assert settings.ideas_repo == tmp_path / "ib"
    assert settings.transcript_language_list == ["en", "nl"]


def test_settings_defaults():
    settings = Settings()
    assert settings.profiles_file == Path("profiles.yaml")
    assert settings.claude_bin == "claude"
