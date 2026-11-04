#!/bin/sh
# Lambda entry point (guide Chapter 4, part 2).
#
# The Lambda Web Adapter (a layer) runs this script, waits until the app
# answers its readiness check, then turns each Lambda invocation into an
# ordinary HTTP request to localhost. The app is the same FastAPI app; it
# doesn't know it's on Lambda.
#
# Demo shortcut: SQLite in /tmp, created on every cold start. Each Lambda
# instance gets its own empty database, and it vanishes when the instance is
# recycled: exactly the statelessness lesson. A real deployment uses RDS
# (through RDS Proxy) or another external database.
set -eu
cd /var/task
python -c "import app.models; from app.db import Base, engine; Base.metadata.create_all(engine)"
python -m app.seed
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "${AWS_LWA_PORT:-8080}" --proxy-headers
