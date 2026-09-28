import json

from catcher.modules.llm.service import BackendReply, Usage

_YOUTUBE = {
    "title": "Fake Video Summary",
    "creator": "Fake Creator",
    "description": "A canned YouTube summary used in tests.",
    "summary": "The video explains a simple system.",
    "main_purpose": "Show a repeatable system.",
    "key_examples": ["Example one"],
    "action_plan": ["Do step one"],
    "tools": ["Obsidian"],
    "tips": [{"tip": "Start small", "explanation": "Small steps stick.", "how_to_apply": "Pick one habit."}],
    "channel_application": "Use it for a demo video.",
    "tags": ["youtube-idea", "content-creation"],
}

CANNED: dict[str, dict] = {
    "note": {
        "title": "Fake Note",
        "description": "A canned note summary used in tests.",
        "body": "A cleaned-up idea.",
        "tags": ["app-idea", "automation"],
    },
    "ai-chat": {
        "title": "Fake Chat",
        "description": "A canned chat summary used in tests.",
        "summary": ["Point one"],
        "decisions": ["Decision one"],
        "options": [],
        "open_questions": [],
        "body": "## 🧩 Details\n\nMore detail.",
        "tags": ["tech-note", "ai-agents"],
    },
    "web-clip": {
        "title": "Fake Web Clip",
        "description": "A canned web clip summary used in tests.",
        "summary": ["Point one"],
        "key_points": ["A fact"],
        "ideas_to_use": [],
        "body": "## 🧩 Details\n\nMore detail.",
        "tags": ["tech-note", "hugo"],
    },
    "youtube": _YOUTUBE,
    "youtube-gemini": _YOUTUBE,
}


class FakeBackend:
    name = "fake"

    def __init__(self, replies: list[str | Exception] | None = None) -> None:
        self.replies: list[str | Exception] = list(replies or [])
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        self.prompts.append(prompt)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            text = reply
        else:
            text = json.dumps(CANNED[task])
        return BackendReply(
            text=text, usage=Usage(tokens_in=len(prompt) // 4, tokens_out=len(text) // 4), model="fake"
        )
