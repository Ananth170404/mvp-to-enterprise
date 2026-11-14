"""The queries behind the invoice list, in one place.

The app and scripts/explain_queries.py both build them from here, so what we
measure and tune is exactly what production runs (guide Chapter 5).
"""

from sqlalchemy import Select, func, select

from app.models import ExtractionRun, Invoice

PAGE_SIZE = 200


def invoice_page(tenant_id: str, status: str | None = None) -> Select:
    """The newest invoices for a tenant, optionally in one status."""
    query = select(Invoice).where(Invoice.tenant_id == tenant_id)
    if status:
        query = query.where(Invoice.status == status)
    return query.order_by(Invoice.created_at.desc()).limit(PAGE_SIZE)


def status_counts(tenant_id: str) -> Select:
    """How many invoices the tenant has in each status (the tabs on the list page)."""
    return select(Invoice.status, func.count()).where(Invoice.tenant_id == tenant_id).group_by(Invoice.status)


def possible_duplicates(tenant_id: str, invoice_id: str, vendor_tax_id: str) -> Select:
    """Other live invoices from the same vendor; the duplicate checks narrow it further."""
    return select(Invoice).where(
        Invoice.tenant_id == tenant_id,
        Invoice.id != invoice_id,
        Invoice.vendor_tax_id == vendor_tax_id,
        Invoice.status != "rejected",
    )


def total_spend(tenant_id: str) -> Select:
    """The tenant's LLM spend so far."""
    return select(func.coalesce(func.sum(ExtractionRun.cost_usd), 0)).where(ExtractionRun.tenant_id == tenant_id)
