"""The contract between Ledgerline and the model.

Amounts are requested as decimal *strings* ("1234.50"), never JSON numbers,
so they are never parsed as floats. The model's output is validated against
this schema; anything that doesn't fit is a failed extraction, not a guess.
"""

import re
from datetime import date

from pydantic import BaseModel, Field, field_validator

PROMPT_VERSION = "extract-v1"

_AMOUNT = re.compile(r"^-?\d+(\.\d+)?$")


def _clean_amount(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", "").replace("₹", "").replace("INR", "").strip()
    if cleaned == "":
        return None
    if not _AMOUNT.match(cleaned):
        raise ValueError(f"not a decimal amount: {value!r}")
    return cleaned


class ExtractedLine(BaseModel):
    description: str
    quantity: str = Field(description="Decimal string, e.g. '2' or '1.5'")
    unit_price: str = Field(description="Decimal string, excluding tax, e.g. '1250.00'")
    tax_rate_percent: str | None = Field(default=None, description="GST rate, e.g. '18'")
    amount: str = Field(description="Line amount excluding tax, decimal string")

    _clean = field_validator("quantity", "unit_price", "tax_rate_percent", "amount", mode="before")(
        lambda cls, v: _clean_amount(v)
    )


class ExtractedInvoice(BaseModel):
    vendor_name: str | None
    vendor_tax_id: str | None = Field(description="Vendor GSTIN, 15 characters, or null")
    invoice_number: str | None
    invoice_date: date | None = Field(description="ISO date, YYYY-MM-DD")
    po_number: str | None = Field(description="Purchase order number referenced on the invoice, or null")
    currency: str = Field(default="INR", description="ISO 4217 code")
    line_items: list[ExtractedLine]
    subtotal: str | None = Field(description="Total before tax, decimal string")
    tax_total: str | None = Field(description="Total tax (CGST+SGST or IGST), decimal string")
    total: str | None = Field(description="Grand total payable, decimal string")

    _clean = field_validator("subtotal", "tax_total", "total", mode="before")(
        lambda cls, v: _clean_amount(v)
    )


EXTRACTION_PROMPT = """You are extracting data from a vendor invoice for an accounts-payable system.

Return only what is printed on the document. Rules:
- If a field is not present, return null. Never guess or calculate a missing value.
- Amounts: decimal strings without currency symbols or thousands separators, e.g. "12500.00".
- invoice_date: ISO format YYYY-MM-DD. Indian invoices usually print dates as DD/MM/YYYY.
- vendor_tax_id: the SELLER's GSTIN (the party issuing the invoice), not the buyer's.
- line_items: one entry per product or service line, amounts excluding tax.
- tax_total: the sum of all tax (CGST + SGST, or IGST).
- If the printed totals look arithmetically wrong, still return the printed values.
"""


def json_schema() -> dict:
    return ExtractedInvoice.model_json_schema()
