from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # where `profiles.yaml` lives; the repos sit beside it


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # The defaults hang off the project folder, not off the folder the command is run from.
    ideas_repo: Path = PROJECT_ROOT.parent / "idea-bucket"
    docs_repo: Path = PROJECT_ROOT.parent / "epiaku-docs"
    profiles_file: Path = PROJECT_ROOT / "profiles.yaml"
    freellmapi_url: str = "http://localhost:3001/v1"
    freellmapi_model: str = "auto"
    freellmapi_api_key: SecretStr = SecretStr("not-needed")  # SecretStr: a repr or a traceback shows ****
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    llm_timeout_s: int = 600
    llm_max_attempts: int = 5  # calls per request on a transient error (5xx, timeout); 1 turns retrying off
    llm_retry_wait_s: float = 2.0  # wait before the 2nd attempt, doubled before each one after it
    llm_max_input_chars: int = 400_000  # a document (with its facts) longer than this is not sent to the LLM
    llm_trace: bool = True  # every LLM call of a run leaves a trace in `llm/` of the idea-bucket
    llm_trace_prompt: bool = False  # also save the full prompt in the trace (large; it holds the document)
    transcript_languages: str = "en"
    # How we talk to YouTube. A ban is per IP and about request rate, so we go slowly and stop when told no.
    youtube_request_delay_s: float = 10.0  # seconds between the requests inside one fetch
    youtube_min_gap_s: float = 120.0  # minimum seconds between the start of two fetches (2 minutes)
    youtube_gap_jitter_s: float = 300.0  # up to this many random extra seconds on the gap
    youtube_block_hours: float = 6.0  # wait after the first block; doubles each time, at most 24 hours
    youtube_offline: bool = False  # never call YouTube, use the saved facts only
    youtube_skip_manifests: bool = False  # skip the video-format request (try by hand before turning on)
    youtube_wait_max_s: float = 1800.0  # with --wait-youtube: the longest wait for the gap inside a run
    youtube_negative_ttl_h: float = 24.0  # a video without captions is asked about again after this long
    catcher_state_dir: Path = Path("~/.catcher/state")  # this machine's state (the YouTube gate); not in git
    git_author_name: str = "idea-catcher"
    git_author_email: str = "idea-catcher@users.noreply.github.com"
    artifact_max_mb: int = 25
    database_url: str = "postgresql+psycopg://catcher:catcher@localhost:5432/catcher"
    log_level: str = "INFO"
    log_file: Path | None = None

    @property
    def transcript_language_list(self) -> list[str]:
        return [lang.strip() for lang in self.transcript_languages.split(",") if lang.strip()]
