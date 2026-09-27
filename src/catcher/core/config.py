from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    ideas_repo: Path = Path("../idea-bucket")
    docs_repo: Path = Path("../epiaku-docs")
    profiles_file: Path = Path("profiles.yaml")
    freellmapi_url: str = "http://localhost:3001/v1"
    freellmapi_model: str = "auto"
    freellmapi_api_key: str = "not-needed"
    claude_bin: str = "claude"
    llm_timeout_s: int = 600
    transcript_languages: str = "en"
    git_author_name: str = "idea-catcher"
    git_author_email: str = "idea-catcher@users.noreply.github.com"

    @property
    def transcript_language_list(self) -> list[str]:
        return [lang.strip() for lang in self.transcript_languages.split(",") if lang.strip()]
