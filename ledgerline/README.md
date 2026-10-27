# Ledgerline

An AI accounts-payable assistant: upload a vendor invoice, an LLM extracts the data, plain code checks it (arithmetic, GSTIN checksum, purchase-order match, duplicates), a person reviews and approves, and every step lands in an append-only audit log.

This is the reference implementation for the guide. It grows chapter by chapter:

| Version | Chapter | What it adds |
|---|---|---|
| v0.1 | [1. The honest MVP](../guide/01-honest-mvp.md) | Core flow, data model, LLM wrapper, checks, audit log, synthetic data, evals |
| v0.2 | [2. Production readiness](../guide/02-production-readiness.md) | Migrations as a separate step, least-privilege DB user, recovery of interrupted work, liveness/readiness, S3 storage, pinned builds, CI, secret scanning, backup drill |

## Run it

You need Docker. No API key is needed to start: the default `fake` provider answers from the synthetic data's ground truth.

```bash
cd ledgerline
cp .env.example .env
mkdir -p data
docker compose run --rm --no-deps api python -m scripts.generate_invoices   # 30 synthetic invoices + POs
docker compose up --build
```

`docker compose up` starts Postgres, runs the one-off `migrate` job (migrations, then the restricted app user, then demo data), and only then starts the API. Open http://localhost:8000 and sign in as **Priya (clerk)** or **Arjun (approver)**.

On Linux, if the generator can't write to `data/`, run `sudo chown 1000 data` (the container runs as a non-root user).

Things to try:

- Upload `013-po_mismatch-…pdf` and read the checks.
- Upload the same file twice: you land on the existing invoice.
- Upload a `duplicate-…` file after its original: flagged as a duplicate.
- Approve an invoice with errors as Arjun: you have to give a reason, and it's in the audit trail.
- Switch to **Meera (Globex)**: she sees none of Acme's invoices.
- Stop the database (`docker compose stop db`) and compare http://localhost:8000/livez with http://localhost:8000/readyz.

## Use a real model

```bash
# .env
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.5-flash-lite      # or gemini-3.8-flash, gemini-3.1-flash-lite
GEMINI_API_KEY=your-key
```

A free-tier key is fine for the synthetic invoices. Google may use free-tier inputs to improve its products, so **never send real invoices on a free key**.

## Measure the model

```bash
docker compose run --rm --no-deps api python -m scripts.run_evals --provider gemini \
  --models gemini-3.5-flash-lite,gemini-3.8-flash --sleep 4
```

Prints field-level accuracy, accuracy per scenario, accuracy on valid outputs only, failed calls grouped by cause (rate limit, retired model, timeout, …), cost per invoice and latency. Results are saved to `data/eval-results/`. Add `--min-accuracy 0.9` to make it exit with an error below 90%, as CI does.

## Operational tasks

```bash
# Backup and restore drill: dump, restore into a scratch DB, verify, time it
docker compose run --rm api python -m scripts.backup_drill

# Fail (and retry) extractions interrupted by a crash or deploy
docker compose run --rm api python -m app.maintenance recover-stuck --retry

# Re-apply the least-privilege app user (safe to repeat)
docker compose run --rm api python -m app.db_roles
```

## Checks before code reaches the repo

```bash
pip install pre-commit && pre-commit install   # from the repository root
```

Every commit is then scanned for secrets (gitleaks) and linted (ruff). CI (`.github/workflows/ci.yml`) runs the same checks plus tests, migrations on real Postgres, a dependency audit and a production image build.

## Tests

```bash
docker compose run --rm --no-deps api pytest -q
```

Tests use SQLite, moto (fake S3) and the fake LLM provider, so they need no database, cloud account or key.

## Dependencies

`requirements.in` and `requirements-dev.in` list what we depend on; `requirements.txt` and `requirements-dev.txt` are the exact, locked versions every build installs. To change a dependency, edit the `.in` file and regenerate:

```bash
uv pip compile requirements.in --python-version 3.12 -o requirements.txt
uv pip compile requirements-dev.in --python-version 3.12 -o requirements-dev.txt
```

## Layout

```
app/
  main.py              routes, health checks, staging password gate, startup recovery
  models.py            data model (tenant_id everywhere, money in minor units)
  extraction.py        the background job: lock, call model, validate, check, transition
  extraction_schema.py the contract with the model (schema + prompt, versioned)
  checks.py            deterministic checks: arithmetic, GSTIN, PO match, duplicates
  workflow.py          invoice state machine
  maintenance.py       recovery of interrupted extractions
  db_roles.py          creates the least-privilege app database user
  llm/                 provider wrapper: gemini, fake, pricing table
  auth.py              DEV-ONLY login (replaced in Chapter 7)
  storage.py           local disk or S3, same interface
migrations/            Alembic; advisory-locked; audit log protected by a DB trigger
scripts/
  generate_invoices.py synthetic GST invoices with ground truth
  run_evals.py         accuracy / cost / latency report, CI gate
  backup_drill.py      backup + restore + verification + timing
  db_roles.sql         the app user's permissions
tests/
Dockerfile             `runtime` image (ships) and `dev` image (compose)
```

## What it deliberately doesn't do yet

Real authentication, a job queue, observability beyond logs, email ingestion, ERP integration, payments, agents taking actions. Each has a chapter.
