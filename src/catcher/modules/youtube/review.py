import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from catcher.modules.llm.profiles import ProfilesConfig
from catcher.modules.llm.schemas import Review, ReviewIssue, YoutubeSummary
from catcher.modules.llm.service import BackendFactory, LlmRequest, LlmResult, reason
from catcher.modules.pipeline.tags import TagList
from catcher.modules.youtube.facts import YoutubeFacts, fmt_ts

ReviewStatus = Literal["off", "ok", "fixed", "needs_attention", "limited"]

_TIMESTAMP = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?![\d:])")
_PROPER_WORD = re.compile(r"\b[A-Z][A-Za-z0-9+#-]{2,}")
_ANY_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#-]{3,}")


@dataclass
class ReviewOutcome:
    status: ReviewStatus
    summary: YoutubeSummary
    issues: list[ReviewIssue]
    llm: LlmResult | None
    reviewed_at: str | None

    def high_issues(self) -> list[ReviewIssue]:
        return [issue for issue in self.issues if issue.severity == "high"]

    def frontmatter(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.llm.model if self.llm else None,
            "reviewed_at": self.reviewed_at,
            "issues": [
                {"kind": i.kind, "severity": i.severity, "excerpt": i.excerpt[:200], "fix": i.fix[:300]}
                for i in self.issues
            ],
        }


def not_reviewed(summary: YoutubeSummary) -> ReviewOutcome:
    return ReviewOutcome("off", summary, [], None, None)


def _seconds(match: re.Match[str]) -> int:
    first, second, third = match.groups()
    if third:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def python_checks(summary: YoutubeSummary, facts: YoutubeFacts) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    texts = [
        summary.summary,
        summary.main_purpose,
        summary.channel_application,
        *summary.key_examples,
        *summary.action_plan,
        *(f"{t.tip} {t.explanation} {t.how_to_apply}" for t in summary.tips),
    ]
    if facts.duration_s:
        for text in texts:
            for match in _TIMESTAMP.finditer(text):
                if _seconds(match) > facts.duration_s:
                    end = fmt_ts(facts.duration_s)
                    issues.append(
                        ReviewIssue(
                            kind="wrong_fact",
                            severity="medium",
                            excerpt=text[:200],
                            fix=f"timestamp {match.group(0)} is after the end of the video ({end})",
                        )
                    )
    if facts.transcript:
        source = " ".join(
            part for part in (facts.title, facts.description, facts.transcript_text()) if part
        ).lower()
        for tool in summary.tools:
            plain = tool.replace("*", "").replace("_", " ")
            words = _PROPER_WORD.findall(plain) or _ANY_WORD.findall(plain)
            if words and not any(word.lower() in source for word in words):
                issues.append(
                    ReviewIssue(
                        kind="unsupported_claim",
                        severity="low",
                        excerpt=tool[:200],
                        fix="this tool was not found in the transcript, title or description",
                    )
                )
    return issues


def review_summary(
    summary_text: str,
    facts: YoutubeFacts,
    *,
    profile: str,
    profiles: ProfilesConfig,
    backends: BackendFactory,
    tags: TagList,
    now: datetime | None = None,
) -> ReviewOutcome:
    request = LlmRequest(
        task="review",
        input={
            "summary": summary_text,
            "facts": facts.model_dump(exclude={"transcript"}),
            "transcript": facts.transcript_text(),
            "tags": tags.as_prompt_dict(),
        },
        schema_name="Review",
        profile=profile,
    )
    result = reason(request, profiles=profiles, backends=backends)
    review = cast(Review, result.output)
    issues = [*review.issues, *python_checks(review.revised, facts)]
    status: ReviewStatus
    if review.verdict == "needs_attention":
        status = "needs_attention"
    elif facts.transcript is None:
        status = "limited"
    else:
        status = review.verdict
    reviewed_at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    return ReviewOutcome(status, review.revised, issues, result, reviewed_at)
