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
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    llm_timeout_s: int = 600
    llm_max_attempts: int = 5  # calls per request on a transient error (5xx, timeout); 1 turns retrying off
    llm_retry_wait_s: float = 2.0  # wait before the 2nd attempt, doubled before each one after it
    transcript_languages: str = "en"
    git_author_name: str = "idea-catcher"
    git_author_email: str = "idea-catcher@users.noreply.github.com"
    artifact_max_mb: int = 25
    log_level: str = "INFO"
    log_file: Path | None = None

    @property
    def transcript_language_list(self) -> list[str]:
        return [lang.strip() for lang in self.transcript_languages.split(",") if lang.strip()]
