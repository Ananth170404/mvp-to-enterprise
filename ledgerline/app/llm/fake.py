"""A provider that makes no API calls.

It answers from the synthetic generator's ground truth (looked up by the
file's sha256). Use it to run the whole app with no key and no cost, and in
tests. It is refused when APP_ENV=production.

Unknown files get a deliberately incomplete answer, so the review screen
still has something to show.
"""

import json
from decimal import Decimal
from pathlib import Path

from app.llm.base import Attachment, LLMResult


class FakeProvider:
    name = "fake"

    def __init__(self, manifest_path: Path | None = None, answers: dict[str, dict] | None = None):
        self._answers: dict[str, dict] = dict(answers or {})
        if manifest_path and manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            for item in manifest.get("invoices", []):
                self._answers.setdefault(item["sha256"], item["truth"])

    def generate_json(self, prompt: str, attachments: list[Attachment], json_schema: dict, model: str) -> LLMResult:
        sha = attachments[0].sha256 if attachments else ""
        answer = self._answers.get(
            sha,
            {
                "vendor_name": "Unknown vendor",
                "vendor_tax_id": None,
                "invoice_number": None,
                "invoice_date": None,
                "po_number": None,
                "currency": "INR",
                "line_items": [],
                "subtotal": None,
                "tax_total": None,
                "total": None,
            },
        )
        return LLMResult(
            text=json.dumps(answer),
            provider=self.name,
            model=f"fake/{model}",
            input_tokens=0,
            output_tokens=0,
            latency_ms=1,
            cost_usd=Decimal("0"),
        )
