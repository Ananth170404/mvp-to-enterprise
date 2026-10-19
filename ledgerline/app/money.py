"""Money is stored as integer minor units (paise, cents) plus a currency code.

Never use floats for money: 0.1 + 0.2 != 0.3 in binary floating point.
All parsing goes through Decimal, and rounding is explicit.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Currencies with a non-standard number of decimal places. Default is 2.
_MINOR_DIGITS = {"JPY": 0, "KRW": 0, "BHD": 3, "KWD": 3}


def minor_digits(currency: str) -> int:
    return _MINOR_DIGITS.get(currency.upper(), 2)


def to_minor(amount: str | int | Decimal, currency: str) -> int:
    """'1,234.50' INR -> 123450. Accepts strings, ints or Decimals; never floats."""
    if isinstance(amount, float):
        raise TypeError("Refusing to convert a float to money; pass a string or Decimal.")
    if isinstance(amount, str):
        amount = amount.replace(",", "").replace("₹", "").replace("$", "").strip()
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError(f"Not a valid amount: {amount!r}") from exc
    scale = Decimal(10) ** minor_digits(currency)
    return int((value * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str) -> Decimal:
    return Decimal(minor) / (Decimal(10) ** minor_digits(currency))


def fmt(minor: int | None, currency: str) -> str:
    if minor is None:
        return "—"
    digits = minor_digits(currency)
    return f"{currency} {from_minor(minor, currency):,.{digits}f}"


def qty(value) -> str:
    """Decimal('1.000') -> '1', Decimal('2.500') -> '2.5'."""
    if value is None:
        return "—"
    return f"{Decimal(value).normalize():f}"
