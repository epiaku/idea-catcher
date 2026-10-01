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
    tags: list[str]


Summary = NoteSummary | ChatSummary | WebClipSummary | YoutubeSummary

SCHEMAS: dict[str, type[BaseModel]] = {
    model.__name__: model for model in (NoteSummary, ChatSummary, WebClipSummary, YoutubeSummary)
}
