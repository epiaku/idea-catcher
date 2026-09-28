from catcher.core.config import Settings
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.backends.openai_compatible import OpenAiCompatibleBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import Backend, BackendUnavailable


def make_backend(profile: Profile, settings: Settings) -> Backend:
    if profile.backend == "fake":
        return FakeBackend()
    if profile.backend == "freellmapi":
        return OpenAiCompatibleBackend(
            "freellmapi",
            settings.freellmapi_url,
            settings.freellmapi_api_key,
            timeout_s=settings.llm_timeout_s,
        )
    if profile.backend == "openai":
        if not settings.openai_api_key:
            raise BackendUnavailable("OPENAI_API_KEY is not set")
        return OpenAiCompatibleBackend(
            "openai",
            settings.openai_base_url,
            settings.openai_api_key,
            json_mode=True,
            timeout_s=settings.llm_timeout_s,
        )
    raise BackendUnavailable(f"backend {profile.backend!r} is not available")
