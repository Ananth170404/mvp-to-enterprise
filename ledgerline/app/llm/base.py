from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Attachment:
    data: bytes
    mime_type: str
    sha256: str


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None  # includes "thinking" tokens: they are billed as output
    latency_ms: int
    cost_usd: Decimal | None  # None when the model isn't in the pricing table


class LLMError(Exception):
    """Raised for any provider failure. `retryable` says whether a retry may help."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    name: str

    def generate_json(
        self,
        prompt: str,
        attachments: list[Attachment],
        json_schema: dict,
        model: str,
    ) -> LLMResult: ...
