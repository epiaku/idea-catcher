from pathlib import Path

from pydantic import Field, SecretStr
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
    llm_cache: bool = True  # read a saved good reply in `llm/` before calling the model
    # worker: a backend that hit a usage limit or was down (or a profile whose model the backend does not
    # know) is not called for this many seconds
    llm_block_s: int = Field(default=600, gt=0)
    # worker: a backend whose budget is used up is not called for this many seconds (6 hours)
    llm_budget_block_s: int = Field(default=21600, gt=0)
    # worker: an item `deferred` for this many days becomes `stuck` (still retried by retry_deferred)
    stuck_after_days: float = Field(default=3, gt=0, allow_inf_nan=False)
    transcript_languages: str = "en"
    # How we talk to YouTube. A ban is per IP and about request rate, so we go slowly and stop when told no.
    youtube_request_delay_s: float = 10.0  # seconds between the requests inside one fetch
    youtube_min_gap_s: float = 120.0  # minimum seconds between the start of two fetches (2 minutes)
    youtube_gap_jitter_s: float = 300.0  # up to this many random extra seconds on the gap
    # wait after the first block; doubles each time, at most 24 hours. More than 0 (0 would turn the breaker
    # off) and at most 24 (the gate's ceiling: a longer value would be read back as damage)
    youtube_block_hours: float = Field(default=6.0, gt=0, le=24)
    youtube_offline: bool = False  # never call YouTube, use the saved facts only
    youtube_skip_manifests: bool = False  # skip the video-format request (try by hand before turning on)
    youtube_wait_max_s: float = 1800.0  # with --wait-youtube: the longest wait for the gap inside a run
    youtube_negative_ttl_h: float = 24.0  # a video without captions is asked about again after this long
    # backfill (`catcher youtube import`): the priority of every job of a released backfill video (below the
    # 0 of a new clip, so new clips go first) and the most videos `--release` lets into inbox/ per 24 hours
    backfill_priority: int = -10
    backfill_daily_limit: int = Field(default=10, ge=0)
    # scheduler (in `catcher worker`): five-field cron strings in `schedule_timezone`; empty = off
    schedule_ideas_pull: str = ""
    schedule_pipeline_run: str = ""
    schedule_publish: str = ""
    schedule_timezone: str = "Europe/Amsterdam"
    schedule_tick_s: float = Field(default=30, gt=0)  # seconds between two checks for a due slot
    git_author_name: str = "idea-catcher"
    git_author_email: str = "idea-catcher@users.noreply.github.com"
    artifact_max_mb: int = 25
    database_url: str = "postgresql+psycopg://catcher:catcher@localhost:5432/catcher"
    log_level: str = "INFO"
    log_file: Path | None = None

    @property
    def transcript_language_list(self) -> list[str]:
        return [lang.strip() for lang in self.transcript_languages.split(",") if lang.strip()]
