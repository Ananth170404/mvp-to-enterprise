"""Import purchase orders from CSV.

Columns: po_number, vendor_gstin, vendor_name, currency, description, quantity, unit_price
One row per PO line. Re-importing the same PO number replaces its lines,
so the import is safe to run twice.
"""

import csv
import io
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import POLine, PurchaseOrder, Vendor
from app.money import to_minor

REQUIRED = {"po_number", "vendor_gstin", "vendor_name", "currency", "description", "quantity", "unit_price"}


class POImportError(ValueError):
    pass


def import_csv(session: Session, tenant_id: str, actor_id: str | None, text: str) -> int:
    reader = csv.DictReader(io.StringIO(text))
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing:
        raise POImportError(f"CSV is missing columns: {', '.join(sorted(missing))}")

    rows_by_po: dict[str, list[dict]] = defaultdict(list)
    for row in reader:
        rows_by_po[row["po_number"].strip()].append(row)

    for po_number, rows in rows_by_po.items():
        first = rows[0]
        vendor = _get_or_create_vendor(session, tenant_id, first["vendor_gstin"].strip().upper(), first["vendor_name"].strip())
        currency = first["currency"].strip().upper() or "INR"
        po = session.scalar(
            select(PurchaseOrder).where(PurchaseOrder.tenant_id == tenant_id, PurchaseOrder.po_number == po_number)
        )
        if po is None:
            po = PurchaseOrder(tenant_id=tenant_id, po_number=po_number, vendor=vendor, currency=currency, total_minor=0)
            session.add(po)
        po.vendor = vendor
        po.lines = [
            POLine(
                position=i,
                description=row["description"].strip(),
                quantity=Decimal(row["quantity"]),
                unit_price_minor=to_minor(row["unit_price"], currency),
            )
            for i, row in enumerate(rows)
        ]
        po.total_minor = sum(int((line.quantity * line.unit_price_minor).to_integral_value()) for line in po.lines)
        session.flush()
        audit.record(
            session, tenant_id=tenant_id, actor_id=actor_id, entity="purchase_order",
            entity_id=po.id, action="imported", after={"po_number": po_number, "lines": len(rows)},
        )
    return len(rows_by_po)


def _get_or_create_vendor(session: Session, tenant_id: str, tax_id: str, name: str) -> Vendor:
    vendor = session.scalar(select(Vendor).where(Vendor.tenant_id == tenant_id, Vendor.tax_id == tax_id))
    if vendor is None:
        vendor = Vendor(tenant_id=tenant_id, tax_id=tax_id, name=name)
        session.add(vendor)
        session.flush()
    return vendor
