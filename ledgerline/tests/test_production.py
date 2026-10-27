"""Chapter 2: behaviour that only matters once the app is deployed."""

import base64
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.maintenance import recover_stuck_extractions
from app.models import AuditEvent, Invoice, utcnow
from tests.helpers import client_for, first, upload


def _invoice_id(response) -> str:
    return response.headers["location"].split("/invoices/")[1].split("?")[0]


def test_liveness_does_not_depend_on_the_database(db, monkeypatch):
    client = TestClient(app)
    assert client.get("/livez").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200

    def broken_connect(*args, **kwargs):
        raise ConnectionError("database is down")

    monkeypatch.setattr("app.main.engine.connect", broken_connect)
    assert client.get("/livez").status_code == 200  # don't restart me...
    assert client.get("/readyz").status_code == 503  # ...but don't send me traffic


def test_interrupted_extraction_is_recovered(db, manifest):
    session, users = db
    invoice_id = _invoice_id(upload(client_for(users["clerk"]), first(manifest, "clean")))

    # Simulate a deploy killing the process mid-extraction 30 minutes ago.
    invoice = session.get(Invoice, invoice_id)
    invoice.status = "extracting"
    invoice.status_changed_at = utcnow() - timedelta(minutes=30)
    session.commit()

    assert recover_stuck_extractions(older_than_minutes=10) == [invoice_id]
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "extraction_failed"
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == invoice_id).order_by(AuditEvent.created_at.desc())
    ).first()
    assert event.action == "status:extraction_failed" and "Interrupted" in event.after["reason"]

    # Retrying works, and the sweep never touches an extraction that is still in progress.
    client_for(users["clerk"]).post(f"/invoices/{invoice_id}/reextract")
    session.expire_all()
    assert session.get(Invoice, invoice_id).status == "needs_review"
    invoice = session.get(Invoice, invoice_id)
    invoice.status, invoice.status_changed_at = "extracting", utcnow()
    session.commit()
    assert recover_stuck_extractions(older_than_minutes=10) == []


def test_site_password_gate(db, monkeypatch):
    from dataclasses import replace

    import app.main as main

    monkeypatch.setattr(main, "settings", replace(main.settings, site_password="s3cret"))
    client = TestClient(app)
    assert client.get("/livez").status_code == 200  # load balancer needs no password
    assert client.get("/login").status_code == 401
    wrong = base64.b64encode(b"ledgerline:nope").decode()
    assert client.get("/login", headers={"Authorization": f"Basic {wrong}"}).status_code == 401
    right = base64.b64encode(b"ledgerline:s3cret").decode()
    assert client.get("/login", headers={"Authorization": f"Basic {right}"}).status_code == 200
