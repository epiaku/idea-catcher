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
