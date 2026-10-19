"""Create demo tenants and users, and import generated purchase orders.

Safe to run repeatedly: it only creates what is missing.
Two tenants exist so you can check that one never sees the other's data.

    python -m app.seed
"""

from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Tenant, User
from app.purchase_orders import import_csv

TENANTS = [
    {
        "name": "Acme Manufacturing Pvt Ltd",
        "tax_id": "33AABCA1234D1ZK",  # Tamil Nadu
        "users": [
            ("priya@acme.example", "Priya (AP clerk)", "clerk"),
            ("arjun@acme.example", "Arjun (Finance approver)", "approver"),
        ],
        "po_csv": Path(__file__).resolve().parent.parent / "data" / "generated" / "purchase_orders.csv",
    },
    {
        "name": "Globex Retail LLP",
        "tax_id": "29AAGFG5678H1ZW",  # Karnataka
        "users": [("meera@globex.example", "Meera (Globex approver)", "approver")],
        "po_csv": None,
    },
]


def seed() -> None:
    session = SessionLocal()
    try:
        for spec in TENANTS:
            tenant = session.scalar(select(Tenant).where(Tenant.name == spec["name"]))
            if tenant is None:
                tenant = Tenant(name=spec["name"], tax_id=spec["tax_id"])
                session.add(tenant)
                session.flush()
                print(f"Created tenant {tenant.name}")
            for email, name, role in spec["users"]:
                if session.scalar(select(User).where(User.email == email)) is None:
                    session.add(User(tenant_id=tenant.id, email=email, name=name, role=role))
                    print(f"  Created user {email} ({role})")
            if spec["po_csv"] and spec["po_csv"].exists():
                count = import_csv(session, tenant.id, None, spec["po_csv"].read_text())
                print(f"  Imported {count} purchase orders from {spec['po_csv'].name}")
        session.commit()
    finally:
        session.close()


if __name__ == "__main__":
    seed()
