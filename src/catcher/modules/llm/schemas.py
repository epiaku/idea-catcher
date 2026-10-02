import re

from pydantic import BaseModel, Field, field_validator


class NoteSummary(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1)
    tags: list[str]
    language: str | None = Field(
        default=None, description="language the note was written in, a two-letter code such as en or nl"
    )

    @field_validator("language", mode="before")
    @classmethod
    def _two_letter_code(cls, value: object) -> str | None:
        """Lower-case two-letter code, or nothing: a bad value must not fail a good page."""
        code = str(value).strip().lower() if value is not None else ""
        return code if re.fullmatch(r"[a-z]{2}", code) else None


class ChatSummary(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    summary: list[str] = Field(min_length=1)
    decisions: list[str]
    options: list[str]
    open_questions: list[str]
    body: str
    tags: list[str]


class WebClipSummary(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    summary: list[str] = Field(min_length=1)
    key_points: list[str]
    ideas_to_use: list[str]
    body: str
    tags: list[str]


class Tip(BaseModel):
    tip: str
    explanation: str
    how_to_apply: str


class Link(BaseModel):
    label: str
    url: str


_CLOCK_TIME = re.compile(r"\d{1,2}:\d{2}(:\d{2})?")


class ChapterItem(BaseModel):
    time: str  # as written, m:ss or h:mm:ss
    title: str


class GeminiMetrics(BaseModel):
    """The numbers in Gemini's own Metrics table, as it wrote them. youtube-gemini only: the direct class
    gets real counts from YouTube. Nothing here is checked."""

    as_of: str | None = None
    views: str | None = None
    likes: str | None = None
    subscribers: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _as_written(cls, value: object) -> str | None:
        """A number becomes text with thousands separators; "Not available" and empty become nothing."""
        if isinstance(value, bool) or value is None:
            return None
        text = f"{value:,}" if isinstance(value, int) else " ".join(str(value).split())
        return None if text.lower() in {"", "not available", "n/a", "null", "none", "unknown"} else text

    @property
    def has_values(self) -> bool:
        return any((self.as_of, self.views, self.likes, self.subscribers))


class YoutubeSummary(BaseModel):
    title: str = Field(min_length=1)
    creator: str
    description: str = Field(min_length=1)
    summary: str
    main_purpose: str
    key_examples: list[str]
    action_plan: list[str]
    tools: list[str]
    tips: list[Tip]
    channel_application: str
    links: list[Link] = []  # picked by the LLM from the video description, checked by code
    metrics: GeminiMetrics | None = None  # youtube-gemini only: as Gemini wrote them
    chapters: list[ChapterItem] = []  # youtube-gemini only: the direct class gets them from yt-dlp

    @field_validator("chapters", mode="before")
    @classmethod
    def _keep_the_usable_chapters(cls, value: object) -> list[object]:
        """Drop a chapter without a clock time or a title instead of failing the whole page over it."""
        items = value if isinstance(value, list) else []
        return [
            {"time": str(i["time"]).strip(), "title": " ".join(str(i["title"]).split())}
            for i in items
            if isinstance(i, dict)
            and _CLOCK_TIME.fullmatch(str(i.get("time", "")).strip())
            and str(i.get("title", "")).strip()
        ]

    tags: list[str]


Summary = NoteSummary | ChatSummary | WebClipSummary | YoutubeSummary

SCHEMAS: dict[str, type[BaseModel]] = {
    model.__name__: model for model in (NoteSummary, ChatSummary, WebClipSummary, YoutubeSummary)
}
