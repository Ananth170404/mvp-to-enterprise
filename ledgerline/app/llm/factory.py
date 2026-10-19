from functools import lru_cache

from app.config import settings
from app.llm.base import LLMProvider


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    if settings.llm_provider == "gemini":
        from app.llm.gemini import GeminiProvider

        return GeminiProvider(api_key=settings.gemini_api_key or "")
    if settings.llm_provider == "fake":
        from app.llm.fake import FakeProvider

        return FakeProvider(manifest_path=settings.fake_llm_manifest)
    raise RuntimeError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
