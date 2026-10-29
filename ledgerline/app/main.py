"""Ledgerline v0 web app: upload → extract → review → approve.

Server-rendered HTML on purpose: one deployable, no separate frontend build,
which is the right amount of UI for testing the riskiest assumption.
"""

import base64
import hashlib
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import audit, checks, workflow
from app.auth import COOKIE, CurrentUser, LoginRequired, current_user
from app.config import settings
from app.db import engine, get_session
from app.extraction import extract_invoice
from app.maintenance import recover_stuck_extractions
from app.models import AuditEvent, Document, ExtractionRun, Invoice, User
from app.money import fmt, qty, to_minor
from app.purchase_orders import POImportError, import_csv
from app.storage import document_key, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

log = logging.getLogger("ledgerline")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Extractions interrupted by the previous shutdown (deploy, crash) are
    # still marked "extracting". Fail them and retry once, in the background so
    # startup isn't delayed. Errors here must never stop the app starting.
    try:
        for invoice_id in recover_stuck_extractions():
            threading.Thread(target=extract_invoice, args=(invoice_id,), daemon=True).start()
    except Exception:
        log.exception("Startup recovery failed; continuing")
    yield


app = FastAPI(title="Ledgerline", version="0.2.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["fmt"] = fmt
templates.env.globals["qty"] = qty

# Recognise files by their content ("magic bytes"), never by the file name or
# the browser-supplied content type, both of which the uploader controls.
_SIGNATURES = [
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
]


def sniff(data: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in _SIGNATURES:
        if data.startswith(magic):
            return mime, ext
    return None


UNGATED_PATHS = {"/livez", "/readyz"}


@app.middleware("http")
async def site_password_gate(request: Request, call_next):
    """A shared password in front of a non-production deploy (HTTP Basic auth).

    The cheap version of "staging must not be public". It sits in front of
    everything except health checks, which the load balancer calls without it.
    Replaced by real authentication in Chapter 7.
    """
    if settings.site_password and request.url.path not in UNGATED_PATHS:
        supplied = ""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode().partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                supplied = ""
        # compare_digest takes the same time whatever the input: no timing leaks.
        if not secrets.compare_digest(supplied.encode(), settings.site_password.encode()):
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Ledgerline staging"'})
    return await call_next(request)


@app.exception_handler(LoginRequired)
def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


def get_invoice(session: Session, user: CurrentUser, invoice_id: str) -> Invoice:
    # Tenant isolation: every lookup filters by the user's tenant. Another
    # tenant's invoice is indistinguishable from one that doesn't exist (404).
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == user.tenant_id))
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def snapshot(inv: Invoice) -> dict:
    return {
        "vendor_name": inv.vendor_name,
        "vendor_tax_id": inv.vendor_tax_id,
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "po_number": inv.po_number,
        "subtotal_minor": inv.subtotal_minor,
        "tax_minor": inv.tax_minor,
        "total_minor": inv.total_minor,
    }


# ---------------------------------------------------------------- health & login


@app.get("/livez")
def livez():
    """Liveness: is this process able to answer at all?

    Deliberately checks nothing else. If it checked the database, a two-minute
    database blip would make the platform restart every healthy container,
    turning a small outage into a big one.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness: should this instance receive traffic right now?

    Checks dependencies. A failing readiness check takes the instance out of
    the load balancer without killing it; it rejoins when the check passes.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        log.exception("Readiness check failed: database unreachable")
        return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
    return {"status": "ok", "database": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/invoices", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)):
    if not settings.dev_login:
        raise HTTPException(status_code=404)
    users = session.scalars(select(User).order_by(User.tenant_id, User.role)).all()
    return templates.TemplateResponse(request, "login.html", {"users": users})


@app.post("/login")
def login(user_id: str = Form(...), session: Session = Depends(get_session)):
    if not settings.dev_login or session.get(User, user_id) is None:
        raise HTTPException(status_code=400)
    response = RedirectResponse("/invoices", status_code=303)
    # response.set_cookie(COOKIE, user_id, httponly=True, samesite="lax")
    response.set_cookie(COOKIE, user_id, httponly=True, samesite="lax", secure=settings.is_deployed)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


# ---------------------------------------------------------------- invoices


@app.get("/invoices", response_class=HTMLResponse)
def list_invoices(
    request: Request,
    status: str | None = None,
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    query = select(Invoice).where(Invoice.tenant_id == user.tenant_id).order_by(Invoice.created_at.desc())
    if status:
        query = query.where(Invoice.status == status)
    invoices = session.scalars(query.limit(200)).all()
    counts = dict(
        session.execute(
            select(Invoice.status, func.count()).where(Invoice.tenant_id == user.tenant_id).group_by(Invoice.status)
        ).all()
    )
    spend = session.scalar(
        select(func.coalesce(func.sum(ExtractionRun.cost_usd), 0)).where(ExtractionRun.tenant_id == user.tenant_id)
    )
    return templates.TemplateResponse(
        request,
        "invoices.html",
        {"user": user, "invoices": invoices, "counts": counts, "status": status, "spend": spend,
         "message": request.query_params.get("message")},
    )


@app.post("/invoices")
async def upload_invoice(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File too large")
    kind = sniff(data)
    if kind is None:
        raise HTTPException(status_code=415, detail="Only PDF, PNG and JPEG files are accepted")
    mime, ext = kind
    sha = hashlib.sha256(data).hexdigest()

    # Idempotent upload: the same file from the same tenant is the same invoice.
    existing = session.scalar(select(Document).where(Document.tenant_id == user.tenant_id, Document.sha256 == sha))
    if existing:
        invoice = session.scalar(select(Invoice).where(Invoice.document_id == existing.id))
        return RedirectResponse(f"/invoices/{invoice.id}?message=This+exact+file+was+already+uploaded", status_code=303)

    key = document_key(user.tenant_id, sha, ext)
    storage.put(key, data, mime)
    doc = Document(
        tenant_id=user.tenant_id, storage_key=key, sha256=sha, mime_type=mime,
        original_filename=(file.filename or "upload")[:500], size_bytes=len(data), uploaded_by=user.id,
    )
    session.add(doc)
    session.flush()
    invoice = Invoice(tenant_id=user.tenant_id, document_id=doc.id, status="uploaded", flags=[])
    session.add(invoice)
    session.flush()
    audit.record(session, tenant_id=user.tenant_id, actor_id=user.id, entity="invoice", entity_id=invoice.id,
                 action="uploaded", after={"filename": doc.original_filename, "sha256": sha})
    session.commit()

    background.add_task(extract_invoice, invoice.id)
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def show_invoice(
    request: Request,
    invoice_id: str,
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    invoice = get_invoice(session, user, invoice_id)
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == user.tenant_id, AuditEvent.entity == "invoice", AuditEvent.entity_id == invoice.id)
        .order_by(AuditEvent.created_at)
    ).all()
    actor_names = {u.id: u.name for u in session.scalars(select(User).where(User.tenant_id == user.tenant_id))}
    return templates.TemplateResponse(
        request,
        "invoice.html",
        {
            "user": user, "invoice": invoice, "events": events, "actor_names": actor_names,
            "has_errors": checks.has_errors(invoice), "message": request.query_params.get("message"),
            "processing": invoice.status in {"uploaded", "extracting"},
        },
    )


@app.get("/invoices/{invoice_id}/document")
def invoice_document(invoice_id: str, user: CurrentUser = Depends(current_user), session: Session = Depends(get_session)):
    invoice = get_invoice(session, user, invoice_id)
    return Response(
        storage.get(invoice.document.storage_key),
        media_type=invoice.document.mime_type,
        headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
    )


def _parse_money(value: str, currency: str) -> int | None:
    value = value.strip()
    return to_minor(value, currency) if value else None


@app.post("/invoices/{invoice_id}/edit")
def edit_invoice(
    invoice_id: str,
    vendor_name: str = Form(""),
    vendor_tax_id: str = Form(""),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    po_number: str = Form(""),
    subtotal: str = Form(""),
    tax_total: str = Form(""),
    total: str = Form(""),
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    invoice = get_invoice(session, user, invoice_id)
    if invoice.status != "needs_review":
        raise HTTPException(status_code=409, detail=f"Invoice is {invoice.status}; only invoices in review can be edited")
    before = snapshot(invoice)
    try:
        invoice.vendor_name = vendor_name.strip() or None
        invoice.vendor_tax_id = vendor_tax_id.strip().upper() or None
        invoice.invoice_number = invoice_number.strip() or None
        invoice.invoice_date = date.fromisoformat(invoice_date) if invoice_date.strip() else None
        invoice.po_number = po_number.strip() or None
        invoice.subtotal_minor = _parse_money(subtotal, invoice.currency)
        invoice.tax_minor = _parse_money(tax_total, invoice.currency)
        invoice.total_minor = _parse_money(total, invoice.currency)
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(f"/invoices/{invoice_id}?message={quote_plus(f'Invalid value: {exc}')}", status_code=303)
    after = snapshot(invoice)
    changed = {k for k in after if after[k] != before[k]}
    if changed:
        # Every human correction is recorded with the model's value and the
        # corrected value. These become new eval examples (Chapter 9).
        audit.record(
            session, tenant_id=user.tenant_id, actor_id=user.id, entity="invoice", entity_id=invoice.id,
            action="corrected", before={k: before[k] for k in changed}, after={k: after[k] for k in changed},
        )
    checks.run_checks(session, invoice)
    session.commit()
    msg = f"Saved {len(changed)} correction(s)" if changed else "No changes"
    return RedirectResponse(f"/invoices/{invoice_id}?message={quote_plus(msg)}", status_code=303)


@app.post("/invoices/{invoice_id}/approve")
def approve_invoice(
    invoice_id: str,
    reason: str = Form(""),
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    if not user.can_approve:
        raise HTTPException(status_code=403, detail="Only approvers can approve invoices")
    invoice = get_invoice(session, user, invoice_id)
    if checks.has_errors(invoice) and not reason.strip():
        return RedirectResponse(
            f"/invoices/{invoice_id}?message=This+invoice+has+errors.+Give+a+reason+to+approve+anyway.", status_code=303
        )
    try:
        workflow.transition(session, invoice, "approved", actor_id=user.id, reason=reason.strip() or None)
    except workflow.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return RedirectResponse(f"/invoices/{invoice_id}?message=Approved", status_code=303)


@app.post("/invoices/{invoice_id}/reject")
def reject_invoice(
    invoice_id: str,
    reason: str = Form(...),
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    if not user.can_approve:
        raise HTTPException(status_code=403, detail="Only approvers can reject invoices")
    if not reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required")
    invoice = get_invoice(session, user, invoice_id)
    try:
        workflow.transition(session, invoice, "rejected", actor_id=user.id, reason=reason.strip())
    except workflow.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return RedirectResponse(f"/invoices/{invoice_id}?message=Rejected", status_code=303)


@app.post("/invoices/{invoice_id}/reextract")
def reextract_invoice(
    invoice_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    invoice = get_invoice(session, user, invoice_id)
    if invoice.status not in {"extraction_failed", "needs_review"}:
        raise HTTPException(status_code=409, detail=f"Cannot re-extract an invoice that is {invoice.status}")
    background.add_task(extract_invoice, invoice.id)
    return RedirectResponse(f"/invoices/{invoice_id}?message=Re-extraction+started", status_code=303)


# ---------------------------------------------------------------- purchase orders


@app.post("/purchase-orders/import")
async def import_purchase_orders(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(current_user),
    session: Session = Depends(get_session),
):
    raw = await file.read(5 * 1024 * 1024)
    try:
        count = import_csv(session, user.tenant_id, user.id, raw.decode("utf-8-sig"))
    except (POImportError, UnicodeDecodeError, ValueError) as exc:
        session.rollback()
        return RedirectResponse(f"/invoices?message={quote_plus(f'PO import failed: {exc}')}", status_code=303)
    session.commit()
    return RedirectResponse(f"/invoices?message=Imported+{count}+purchase+orders", status_code=303)
