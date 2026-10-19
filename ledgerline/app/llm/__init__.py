"""The only place in Ledgerline that knows LLM providers exist.

Everything else calls get_provider().generate_json(...). Swapping providers,
models or adding a fallback is a change here, not across the codebase.
"""

from app.llm.base import Attachment, LLMError, LLMProvider, LLMResult
from app.llm.factory import get_provider

__all__ = ["Attachment", "LLMError", "LLMProvider", "LLMResult", "get_provider"]
