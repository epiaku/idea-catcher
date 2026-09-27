from typing import Literal

from pydantic import BaseModel, Field


class NoteSummary(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1)
    tags: list[str]


class ChatSummary(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    summary: list[str] = Field(min_length=1)
    decisions: list[str]
    options: list[str]
    open_questions: list[str]
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


class ReviewIssue(BaseModel):
    kind: Literal["unsupported_claim", "wrong_fact", "missing_point", "wrong_metric", "format"]
    severity: Literal["low", "medium", "high"]
    excerpt: str
    evidence: str | None = None
    fix: str


class Review(BaseModel):
    verdict: Literal["ok", "fixed", "needs_attention"]
    issues: list[ReviewIssue]
    revised: YoutubeSummary


Summary = NoteSummary | ChatSummary | YoutubeSummary

SCHEMAS: dict[str, type[BaseModel]] = {
    model.__name__: model for model in (NoteSummary, ChatSummary, YoutubeSummary, Review)
}
