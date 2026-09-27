from catcher.core.config import Settings
from catcher.modules.llm.backends.claude_code import ClaudeCodeBackend
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import Backend, BackendUnavailable


def make_backend(profile: Profile, settings: Settings) -> Backend:
    if profile.backend == "fake":
        return FakeBackend()
    if profile.backend == "claude-code":
        return ClaudeCodeBackend(settings.claude_bin, timeout_s=settings.llm_timeout_s)
    raise BackendUnavailable(f"backend {profile.backend!r} is not available")
