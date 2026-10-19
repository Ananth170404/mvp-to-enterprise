"""Deterministic checks that run after every extraction and every human edit.

The model reads the document; plain code checks the result. Arithmetic, tax-ID
checksums, PO matching and duplicate detection are things code does reliably
and models don't, so we never ask the model to do them.

Each flag: {"code", "severity": "error" | "warning" | "info", "message"}.
Errors need an explicit reason from the approver to override.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import gstin
from app.models import Invoice, PurchaseOrder, Vendor
from app.money import fmt, qty

# Allow tiny rounding differences (1 rupee) between printed and computed sums.
TOLERANCE_MINOR = 100


def _flag(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def run_checks(session: Session, invoice: Invoice) -> list[dict]:
    flags: list[dict] = []
    flags += _completeness(invoice)
    flags += _tax_id(invoice)
    flags += _arithmetic(invoice)
    flags += _vendor(session, invoice)
    flags += _purchase_order(session, invoice)
    flags += _duplicates(session, invoice)
    invoice.flags = flags
    return flags


def has_errors(invoice: Invoice) -> bool:
    return any(f["severity"] == "error" for f in (invoice.flags or []))


def _completeness(inv: Invoice) -> list[dict]:
    missing = [
        name
        for name, value in [
            ("invoice number", inv.invoice_number),
            ("invoice date", inv.invoice_date),
            ("vendor GSTIN", inv.vendor_tax_id),
            ("total", inv.total_minor),
        ]
        if value in (None, "")
    ]
    if missing:
        return [_flag("missing_fields", "error", "Missing: " + ", ".join(missing))]
    return []


def _tax_id(inv: Invoice) -> list[dict]:
    if inv.vendor_tax_id and not gstin.is_valid(inv.vendor_tax_id):
        return [_flag("invalid_gstin", "error", f"GSTIN {inv.vendor_tax_id} fails format or checksum validation")]
    return []


def _arithmetic(inv: Invoice) -> list[dict]:
    flags = []
    cur = inv.currency
    if inv.lines and inv.subtotal_minor is not None:
        line_sum = sum(line.amount_minor for line in inv.lines)
        if abs(line_sum - inv.subtotal_minor) > TOLERANCE_MINOR:
            flags.append(_flag(
                "lines_sum_mismatch", "error",
                f"Line items add up to {fmt(line_sum, cur)} but subtotal is {fmt(inv.subtotal_minor, cur)}",
            ))
        for line in inv.lines:
            expected = int((Decimal(line.quantity) * line.unit_price_minor).to_integral_value())
            if abs(expected - line.amount_minor) > TOLERANCE_MINOR:
                flags.append(_flag(
                    "line_math_mismatch", "warning",
                    f"Line {line.position + 1}: quantity × unit price is {fmt(expected, cur)}, "
                    f"printed amount is {fmt(line.amount_minor, cur)}",
                ))
        if inv.tax_minor is not None and all(line.tax_rate_percent is not None for line in inv.lines):
            expected_tax = sum(
                int((line.amount_minor * Decimal(line.tax_rate_percent) / 100).to_integral_value())
                for line in inv.lines
            )
            if abs(expected_tax - inv.tax_minor) > TOLERANCE_MINOR:
                flags.append(_flag(
                    "tax_mismatch", "warning",
                    f"Tax at printed rates would be {fmt(expected_tax, cur)}, invoice shows {fmt(inv.tax_minor, cur)}",
                ))
    if None not in (inv.subtotal_minor, inv.tax_minor, inv.total_minor):
        if abs(inv.subtotal_minor + inv.tax_minor - inv.total_minor) > TOLERANCE_MINOR:
            flags.append(_flag(
                "total_mismatch", "error",
                f"Subtotal + tax = {fmt(inv.subtotal_minor + inv.tax_minor, cur)} but total is {fmt(inv.total_minor, cur)}",
            ))
    return flags


def _vendor(session: Session, inv: Invoice) -> list[dict]:
    inv.vendor_id = None
    if not inv.vendor_tax_id:
        return []
    vendor = session.scalar(
        select(Vendor).where(Vendor.tenant_id == inv.tenant_id, Vendor.tax_id == inv.vendor_tax_id)
    )
    if vendor is None:
        return [_flag("unknown_vendor", "warning", f"No vendor on file with GSTIN {inv.vendor_tax_id}")]
    inv.vendor_id = vendor.id
    return []


def _purchase_order(session: Session, inv: Invoice) -> list[dict]:
    inv.po_id = None
    if not inv.po_number:
        return [_flag("no_po", "warning", "Invoice does not reference a purchase order")]
    po = session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.tenant_id == inv.tenant_id, PurchaseOrder.po_number == inv.po_number)
    )
    if po is None:
        return [_flag("po_not_found", "error", f"Purchase order {inv.po_number} not found")]
    inv.po_id = po.id
    flags = []
    if inv.vendor_id and po.vendor_id != inv.vendor_id:
        flags.append(_flag("po_vendor_mismatch", "error", f"PO {po.po_number} belongs to {po.vendor.name}"))
    if inv.subtotal_minor is not None and abs(po.total_minor - inv.subtotal_minor) > TOLERANCE_MINOR:
        flags.append(_flag(
            "po_amount_mismatch", "error",
            f"Invoice subtotal {fmt(inv.subtotal_minor, inv.currency)} differs from PO {po.po_number} "
            f"total {fmt(po.total_minor, po.currency)}",
        ))
    for inv_line, po_line in zip(inv.lines, po.lines):
        if Decimal(inv_line.quantity) != Decimal(po_line.quantity) or inv_line.unit_price_minor != po_line.unit_price_minor:
            flags.append(_flag(
                "po_line_mismatch", "warning",
                f"Line {inv_line.position + 1}: invoiced {qty(inv_line.quantity)} × {fmt(inv_line.unit_price_minor, inv.currency)}, "
                f"PO says {qty(po_line.quantity)} × {fmt(po_line.unit_price_minor, po.currency)}",
            ))
    if len(inv.lines) != len(po.lines):
        flags.append(_flag(
            "po_line_count_mismatch", "warning",
            f"Invoice has {len(inv.lines)} lines, PO {po.po_number} has {len(po.lines)}",
        ))
    return flags


def _duplicates(session: Session, inv: Invoice) -> list[dict]:
    if not inv.vendor_tax_id:
        return []
    others = select(Invoice).where(
        Invoice.tenant_id == inv.tenant_id,
        Invoice.id != inv.id,
        Invoice.vendor_tax_id == inv.vendor_tax_id,
        Invoice.status != "rejected",
    )
    flags = []
    if inv.invoice_number:
        same_number = session.scalar(others.where(Invoice.invoice_number == inv.invoice_number))
        if same_number:
            flags.append(_flag(
                "duplicate_invoice_number", "error",
                f"Invoice number {inv.invoice_number} from this vendor already exists (status: {same_number.status})",
            ))
            return flags
    if inv.total_minor is not None and inv.invoice_date is not None:
        same_amount = session.scalar(
            others.where(Invoice.total_minor == inv.total_minor, Invoice.invoice_date == inv.invoice_date)
        )
        if same_amount:
            flags.append(_flag(
                "possible_duplicate", "warning",
                f"Another invoice from this vendor has the same date and total (number {same_amount.invoice_number})",
            ))
    return flags
