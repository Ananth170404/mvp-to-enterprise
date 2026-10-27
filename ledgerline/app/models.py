"""Ledgerline v0 data model.

Deliberate choices (guide Chapter 1, section 1.5):
- Every business table carries tenant_id. Queries always filter by it.
- Money is integer minor units + currency. No floats anywhere.
- Documents carry a sha256 so the same file is never processed twice.
- extraction_runs records model, prompt version, tokens, cost and latency.
- audit_events is insert-only (enforced in app code and by a DB trigger).
- Invoice status is an explicit state machine (see app/workflow.py).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    # UUIDs rather than sequential integers: safe to expose in URLs, don't leak
    # volume, and can be generated anywhere (useful once we have queues).
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    # The tenant's own GSTIN, used to decide CGST+SGST vs IGST.
    tax_id: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # v0 roles: "clerk" can upload and edit, "approver" can also approve.
    role: Mapped[str] = mapped_column(String(20), default="clerk")


class Vendor(Base):
    __tablename__ = "vendors"
    __table_args__ = (UniqueConstraint("tenant_id", "tax_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    tax_id: Mapped[str | None] = mapped_column(String(20))


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("tenant_id", "po_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"))
    po_number: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    total_minor: Mapped[int] = mapped_column(BigInteger)

    vendor: Mapped[Vendor] = relationship()
    lines: Mapped[list["POLine"]] = relationship(
        back_populates="po", cascade="all, delete-orphan", order_by="POLine.position"
    )


class POLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    po_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price_minor: Mapped[int] = mapped_column(BigInteger)

    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class Document(Base):
    __tablename__ = "documents"
    # Same file uploaded twice by the same tenant -> same document. This is the
    # cheapest duplicate check there is, and it makes uploads idempotent.
    __table_args__ = (UniqueConstraint("tenant_id", "sha256"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    storage_key: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(100))
    original_filename: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoices_status_status_changed_at", "status", "status_changed_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="uploaded", index=True)
    # When the status last changed. Lets us find work that started and never
    # finished (e.g. an extraction interrupted by a deploy). Added in migration
    # 0002 as nullable + backfill: the "expand" step of expand/contract.
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)

    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("vendors.id"))
    vendor_name: Mapped[str | None] = mapped_column(String(300))
    vendor_tax_id: Mapped[str | None] = mapped_column(String(20))
    invoice_number: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    po_number: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    subtotal_minor: Mapped[int | None] = mapped_column(BigInteger)
    tax_minor: Mapped[int | None] = mapped_column(BigInteger)
    total_minor: Mapped[int | None] = mapped_column(BigInteger)

    po_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_orders.id"))
    # Machine-generated checks: [{"code": "...", "severity": "...", "message": "..."}]
    flags: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    document: Mapped[Document] = relationship()
    po: Mapped[PurchaseOrder | None] = relationship()
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.position"
    )
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(
        back_populates="invoice", order_by="ExtractionRun.created_at.desc()"
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price_minor: Mapped[int] = mapped_column(BigInteger)
    tax_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    amount_minor: Mapped[int] = mapped_column(BigInteger)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class ExtractionRun(Base):
    """One LLM call. The basis for cost tracking, debugging and evals."""

    __tablename__ = "extraction_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(30))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))  # succeeded | failed
    error: Mapped[str | None] = mapped_column(Text)
    raw_output: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    invoice: Mapped[Invoice] = relationship(back_populates="extraction_runs")


class AuditEvent(Base):
    """Insert-only history of who did what. Never updated, never deleted."""

    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36))  # None = system
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(50))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
