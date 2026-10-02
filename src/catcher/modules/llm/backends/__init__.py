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
            settings.freellmapi_api_key.get_secret_value(),
            timeout_s=settings.llm_timeout_s,
            max_attempts=settings.llm_max_attempts,
            retry_wait_s=settings.llm_retry_wait_s,
        )
    if profile.backend == "openai":
        if not settings.openai_api_key.get_secret_value():
            raise BackendUnavailable("OPENAI_API_KEY is not set")
        return OpenAiCompatibleBackend(
            "openai",
            settings.openai_base_url,
            settings.openai_api_key.get_secret_value(),
            json_mode=True,
            timeout_s=settings.llm_timeout_s,
            max_attempts=settings.llm_max_attempts,
            retry_wait_s=settings.llm_retry_wait_s,
        )
    raise BackendUnavailable(f"backend {profile.backend!r} is not available")
