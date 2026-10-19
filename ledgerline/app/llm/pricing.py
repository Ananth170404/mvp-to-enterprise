"""Model prices, so every call's cost is recorded at the time it happens.

Prices change, sometimes on a schedule announced in advance, so each entry has
an effective-from date. Source: https://ai.google.dev/gemini-api/docs/pricing
(Standard tier, paid; checked 2026-10-19). Re-check before relying on these.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Price:
    effective_from: date
    input_per_million: Decimal
    output_per_million: Decimal


_D = Decimal
PRICES: dict[str, list[Price]] = {
    "gemini-3.5-flash-lite": [
        Price(date(2026, 7, 21), _D("0.30"), _D("2.50")),
    ],
    "gemini-3.1-flash-lite": [
        Price(date(2026, 5, 7), _D("0.25"), _D("1.50")),
    ],
    "gemini-3.8-flash": [
        Price(date(2026, 9, 2), _D("0.75"), _D("3.75")),
        Price(date(2027, 1, 1), _D("1.50"), _D("7.50")),  # announced price rise
    ],
}


def price_for(model: str, on: date) -> Price | None:
    schedule = [p for p in PRICES.get(model, []) if p.effective_from <= on]
    return max(schedule, key=lambda p: p.effective_from) if schedule else None


def cost_usd(model: str, input_tokens: int | None, output_tokens: int | None, on: date) -> Decimal | None:
    price = price_for(model, on)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    million = Decimal(1_000_000)
    return (
        Decimal(input_tokens) * price.input_per_million / million
        + Decimal(output_tokens) * price.output_per_million / million
    ).quantize(Decimal("0.000001"))
