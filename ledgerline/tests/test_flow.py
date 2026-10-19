from sqlalchemy import select

from app.models import AuditEvent, ExtractionRun, Invoice
from tests.helpers import client_for, first, upload


def invoice_from(response, session) -> Invoice:
    assert response.status_code == 303, response.text
    invoice_id = response.headers["location"].split("/invoices/")[1].split("?")[0]
    session.expire_all()
    return session.get(Invoice, invoice_id)


def codes(invoice: Invoice) -> set[str]:
    return {f["code"] for f in invoice.flags}


def test_clean_invoice_extracts_and_passes_checks(db, manifest):
    session, users = db
    item = first(manifest, "clean")
    inv = invoice_from(upload(client_for(users["clerk"]), item), session)

    assert inv.status == "needs_review"
    assert inv.invoice_number == item["truth"]["invoice_number"]
    assert not [f for f in inv.flags if f["severity"] == "error"], inv.flags
    assert inv.po_id is not None and inv.vendor_id is not None
    runs = session.scalars(select(ExtractionRun).where(ExtractionRun.invoice_id == inv.id)).all()
    assert len(runs) == 1 and runs[0].status == "succeeded" and runs[0].prompt_version == "extract-v1"


def test_same_file_twice_is_the_same_invoice(db, manifest):
    session, users = db
    client = client_for(users["clerk"])
    item = first(manifest, "clean")
    a = invoice_from(upload(client, item), session)
    second = upload(client, item)
    assert "already+uploaded" in second.headers["location"]
    assert invoice_from(second, session).id == a.id
    assert len(session.scalars(select(Invoice)).all()) == 1


def test_resent_invoice_is_flagged_as_duplicate(db, manifest):
    session, users = db
    client = client_for(users["clerk"])
    dup = first(manifest, "duplicate")
    original = next(i for i in manifest if i["scenario"] == "clean" and i["truth"]["invoice_number"] == dup["truth"]["invoice_number"])
    invoice_from(upload(client, original), session)
    inv = invoice_from(upload(client, dup), session)
    assert "duplicate_invoice_number" in codes(inv)


def test_scenarios_raise_expected_flags(db, manifest):
    session, users = db
    client = client_for(users["clerk"])
    for scenario in ["po_mismatch", "arithmetic", "no_po"]:
        item = first(manifest, scenario)
        inv = invoice_from(upload(client, item), session)
        assert set(item["expected_flags"]) <= codes(inv), (scenario, inv.flags)


def test_other_tenant_cannot_see_invoice(db, manifest):
    session, users = db
    inv = invoice_from(upload(client_for(users["clerk"]), first(manifest, "clean")), session)
    outsider = client_for(users["other_tenant"])
    assert outsider.get(f"/invoices/{inv.id}").status_code == 404
    assert outsider.get(f"/invoices/{inv.id}/document").status_code == 404
    assert outsider.post(f"/invoices/{inv.id}/approve", data={"reason": "x"}).status_code == 404
    assert inv.vendor_name not in outsider.get("/invoices").text


def test_approval_rules_and_audit_trail(db, manifest):
    session, users = db
    clerk, approver = client_for(users["clerk"]), client_for(users["approver"])
    inv = invoice_from(upload(clerk, first(manifest, "arithmetic")), session)

    assert clerk.post(f"/invoices/{inv.id}/approve", data={}).status_code == 403
    no_reason = approver.post(f"/invoices/{inv.id}/approve", data={"reason": ""}, follow_redirects=False)
    assert "Give+a+reason" in no_reason.headers["location"]
    session.expire_all()
    assert session.get(Invoice, inv.id).status == "needs_review"

    approver.post(f"/invoices/{inv.id}/approve", data={"reason": "Vendor confirmed by phone"})
    session.expire_all()
    assert session.get(Invoice, inv.id).status == "approved"
    # Approved invoices are frozen.
    assert clerk.post(f"/invoices/{inv.id}/edit", data={"invoice_number": "X"}).status_code == 409
    assert approver.post(f"/invoices/{inv.id}/reject", data={"reason": "oops"}).status_code == 409

    actions = [e.action for e in session.scalars(select(AuditEvent).where(AuditEvent.entity_id == inv.id).order_by(AuditEvent.created_at))]
    assert actions == ["uploaded", "status:extracting", "status:needs_review", "status:approved"]


def test_corrections_are_recorded_and_rechecked(db, manifest):
    session, users = db
    clerk = client_for(users["clerk"])
    item = first(manifest, "arithmetic")
    inv = invoice_from(upload(clerk, item), session)
    assert "total_mismatch" in codes(inv)

    t = item["truth"]
    correct_total = f"{float(t['subtotal']) + float(t['tax_total']):.2f}"
    form = {"vendor_name": t["vendor_name"], "vendor_tax_id": t["vendor_tax_id"], "invoice_number": t["invoice_number"],
            "invoice_date": t["invoice_date"], "po_number": t["po_number"] or "", "subtotal": t["subtotal"],
            "tax_total": t["tax_total"], "total": correct_total}
    clerk.post(f"/invoices/{inv.id}/edit", data=form)
    session.expire_all()
    inv = session.get(Invoice, inv.id)
    assert "total_mismatch" not in codes(inv)
    event = session.scalar(select(AuditEvent).where(AuditEvent.entity_id == inv.id, AuditEvent.action == "corrected"))
    assert set(event.before) == {"total_minor"} and event.after["total_minor"] != event.before["total_minor"]


def test_rejects_files_that_are_not_documents(db):
    _, users = db
    response = client_for(users["clerk"]).post("/invoices", files={"file": ("invoice.pdf", b"MZ\x90\x00 not a pdf")})
    assert response.status_code == 415


def test_bad_model_output_marks_extraction_failed(db, manifest, monkeypatch):
    from decimal import Decimal

    from app.llm.base import LLMResult
    from app.llm.factory import get_provider

    class Broken:
        name = "broken"

        def generate_json(self, *args, **kwargs):
            return LLMResult("{not json", "broken", "m", 1, 1, 5, Decimal("0"))

    monkeypatch.setattr("app.extraction.get_provider", lambda: Broken())
    session, users = db
    inv = invoice_from(upload(client_for(users["clerk"]), first(manifest, "clean")), session)
    assert inv.status == "extraction_failed"
    run = session.scalar(select(ExtractionRun).where(ExtractionRun.invoice_id == inv.id))
    assert run.status == "failed" and "Invalid model output" in run.error
    get_provider.cache_clear()


def test_pages_render(db, manifest):
    session, users = db
    client = client_for(users["approver"])
    inv = invoice_from(upload(client, first(manifest, "po_mismatch")), session)
    assert client.get("/invoices").status_code == 200
    page = client.get(f"/invoices/{inv.id}")
    assert page.status_code == 200 and "po_amount_mismatch" in page.text and "Approve" in page.text
    assert client.get(f"/invoices/{inv.id}/document").headers["content-type"] == "application/pdf"
    assert client_for(users["clerk"]).get("/login").status_code == 200
