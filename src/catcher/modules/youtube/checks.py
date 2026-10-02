import re
from dataclasses import dataclass
from typing import Literal

from catcher.modules.llm.schemas import Link, YoutubeSummary
from catcher.modules.youtube.facts import YoutubeFacts, fmt_ts

_TIMESTAMP = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?![\d:])")
_PROPER_WORD = re.compile(r"\b[A-Z][A-Za-z0-9+#-]{2,}")
_ANY_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#-]{3,}")


@dataclass
class SummaryWarning:
    """A free, non-LLM check on a YouTube summary. Never fixes anything, just says where to look."""

    kind: Literal["wrong_timestamp", "unsupported_claim"]
    severity: Literal["low", "medium"]
    excerpt: str
    fix: str

    def frontmatter(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "excerpt": self.excerpt[:200],
            "fix": self.fix[:300],
        }


def _seconds(match: re.Match[str]) -> int:
    first, second, third = match.groups()
    if third:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def _squash(text: str) -> str:
    """Lower case without spaces, hyphens and underscores, to compare names however captions split them."""
    return re.sub(r"[\s_-]+", "", text.lower())


def check_summary(summary: YoutubeSummary, facts: YoutubeFacts) -> list[SummaryWarning]:
    """Check a finished summary against the facts it was given. No LLM call, nothing is fixed."""
    warnings: list[SummaryWarning] = []
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
                    warnings.append(
                        SummaryWarning(
                            kind="wrong_timestamp",
                            severity="medium",
                            excerpt=text[:200],
                            fix=f"timestamp {match.group(0)} is after the end of the video ({end})",
                        )
                    )
    if facts.transcript:
        source = " ".join(
            part for part in (facts.title, facts.description, facts.transcript_text()) if part
        ).lower()
        squashed = _squash(source)  # captions often split a name: "type form" for Typeform
        for tool in summary.tools:
            plain = tool.replace("*", "").replace("_", " ")
            words = _PROPER_WORD.findall(plain) or _ANY_WORD.findall(plain)
            if words and not any(word.lower() in source or _squash(word) in squashed for word in words):
                warnings.append(
                    SummaryWarning(
                        kind="unsupported_claim",
                        severity="low",
                        excerpt=tool[:200],
                        fix="this tool was not found in the transcript, title or description",
                    )
                )
    return warnings


MAX_LINKS = 6


def verified_links(links: list[Link], description: str | None) -> list[Link]:
    """Keep only the links whose URL really is in the video description, so a link the LLM made up never
    reaches a page. At most MAX_LINKS, no duplicates, and a plain one-line label."""
    kept: list[Link] = []
    for link in links:
        url = link.url.strip()
        label = " ".join(link.label.replace("[", "(").replace("]", ")").split())
        if not (url.startswith(("http://", "https://")) and label and description and url in description):
            continue
        if all(url != k.url for k in kept):
            kept.append(Link(label=label, url=url))
    return kept[:MAX_LINKS]
