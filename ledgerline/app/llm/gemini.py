import time
from datetime import date

from google import genai
from google.genai import errors, types

from app.llm.base import Attachment, LLMError, LLMResult
from app.llm.pricing import cost_usd


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, timeout_s: int = 120):
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_s * 1000),
        )

    def generate_json(
        self,
        prompt: str,
        attachments: list[Attachment],
        json_schema: dict,
        model: str,
    ) -> LLMResult:
        contents: list = [types.Part.from_bytes(data=a.data, mime_type=a.mime_type) for a in attachments]
        contents.append(prompt)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=json_schema,
            temperature=0,  # extraction should be as repeatable as possible
        )

        started = time.monotonic()
        try:
            response = self._client.models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as exc:
            # 429 (rate limit) and 5xx are worth retrying; 400/403/404 are not.
            # A 404 here usually means the model ID was retired.
            code = getattr(exc, "code", None) or 0
            raise LLMError(f"Gemini API error {code}: {exc}", retryable=code == 429 or code >= 500) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count if usage else None
        output_tokens = None
        if usage:
            output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)

        text = response.text
        if not text:
            raise LLMError("Gemini returned an empty response.", retryable=True)

        return LLMResult(
            text=text,
            provider=self.name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd(model, input_tokens, output_tokens, date.today()),
        )
