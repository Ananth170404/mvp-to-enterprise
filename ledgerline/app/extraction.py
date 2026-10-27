"""Turn an uploaded document into structured invoice data.

extract_invoice() is the unit of background work. Today FastAPI runs it as a
background task in the same process. In Chapter 6 it moves to a Celery worker:
only the *caller* changes, because this function takes an ID, opens its own
session and is safe to run twice. If the process dies mid-way, the invoice
stays in "extracting"; app.maintenance finds and recovers it.
"""

import json
import logging
import time
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import checks, workflow
from app.config import settings
from app.db import SessionLocal
from app.extraction_schema import EXTRACTION_PROMPT, PROMPT_VERSION, ExtractedInvoice, json_schema
from app.llm import Attachment, LLMError, get_provider
from app.models import ExtractionRun, Invoice, InvoiceLine
from app.money import to_minor
from app.storage import storage

log = logging.getLogger("ledgerline.extraction")

MAX_ATTEMPTS = 3


def extract_invoice(invoice_id: str, session_factory=SessionLocal) -> None:
    session: Session = session_factory()
    try:
        # Lock the row: if two workers (or a retry and a recovery sweep) pick up
        # the same invoice, the second waits here, then sees "extracting" and skips.
        invoice = session.get(Invoice, invoice_id, with_for_update=True)
        if invoice is None:
            log.warning("extract_invoice: invoice %s not found", invoice_id)
            return
        if invoice.status not in {"uploaded", "extraction_failed", "needs_review"}:
            # Already being processed or finished: running twice is a no-op.
            log.info("extract_invoice: skipping %s in status %s", invoice_id, invoice.status)
            return
        workflow.transition(session, invoice, "extracting", actor_id=None)
        session.commit()

        doc = invoice.document
        attachment = Attachment(data=storage.get(doc.storage_key), mime_type=doc.mime_type, sha256=doc.sha256)
        extracted = _call_model(session, invoice, attachment)

        if extracted is None:
            workflow.transition(session, invoice, "extraction_failed", actor_id=None)
        else:
            apply_extraction(invoice, extracted)
            checks.run_checks(session, invoice)
            workflow.transition(session, invoice, "needs_review", actor_id=None)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("extract_invoice: unexpected failure for %s", invoice_id)
        raise
    finally:
        session.close()


def _call_model(session: Session, invoice: Invoice, attachment: Attachment) -> ExtractedInvoice | None:
    provider = get_provider()
    model = settings.llm_model
    for attempt in range(1, MAX_ATTEMPTS + 1):
        run = ExtractionRun(
            tenant_id=invoice.tenant_id,
            invoice_id=invoice.id,
            provider=provider.name,
            model=model,
            prompt_version=PROMPT_VERSION,
        )
        try:
            result = provider.generate_json(EXTRACTION_PROMPT, [attachment], json_schema(), model)
            run.model = result.model
            run.input_tokens = result.input_tokens
            run.output_tokens = result.output_tokens
            run.cost_usd = result.cost_usd
            run.latency_ms = result.latency_ms
            run.raw_output = result.text
            extracted = ExtractedInvoice.model_validate(json.loads(result.text))
            run.status = "succeeded"
            session.add(run)
            session.commit()
            return extracted
        except LLMError as exc:
            run.status, run.error = "failed", str(exc)
            session.add(run)
            session.commit()
            if not exc.retryable or attempt == MAX_ATTEMPTS:
                return None
            time.sleep(2**attempt)  # exponential backoff: 2s, 4s
        except (json.JSONDecodeError, ValidationError) as exc:
            # The model answered, but not in the agreed shape. Record it and stop:
            # retrying with the same prompt at temperature 0 rarely helps.
            run.status, run.error = "failed", f"Invalid model output: {exc}"
            session.add(run)
            session.commit()
            return None
    return None


def apply_extraction(invoice: Invoice, data: ExtractedInvoice) -> None:
    cur = (data.currency or "INR").upper()

    def money(value: str | None) -> int | None:
        return None if value is None else to_minor(value, cur)

    invoice.vendor_name = data.vendor_name
    invoice.vendor_tax_id = data.vendor_tax_id.strip().upper() if data.vendor_tax_id else None
    invoice.invoice_number = data.invoice_number.strip() if data.invoice_number else None
    invoice.invoice_date = data.invoice_date
    invoice.po_number = data.po_number.strip() if data.po_number else None
    invoice.currency = cur
    invoice.subtotal_minor = money(data.subtotal)
    invoice.tax_minor = money(data.tax_total)
    invoice.total_minor = money(data.total)
    invoice.lines = [
        InvoiceLine(
            position=i,
            description=line.description,
            quantity=Decimal(line.quantity),
            unit_price_minor=to_minor(line.unit_price, cur),
            tax_rate_percent=Decimal(line.tax_rate_percent) if line.tax_rate_percent else None,
            amount_minor=to_minor(line.amount, cur),
        )
        for i, line in enumerate(data.line_items)
    ]
