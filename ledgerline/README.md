# Ledgerline v0

An AI accounts-payable assistant: upload a vendor invoice, an LLM extracts the data, plain code checks it (arithmetic, GSTIN checksum, purchase-order match, duplicates), a person reviews and approves, and every step lands in an append-only audit log.

This is the reference implementation for [Chapter 1 of the guide](../guide/01-honest-mvp.md). It is deliberately an **MVP**: one service, one database, no queue, dev-only login.

## Run it

You need Docker. No API key is needed to start: the default `fake` provider answers from the synthetic data's ground truth.

```bash
cd ledgerline
cp .env.example .env
docker compose run --rm api python -m scripts.generate_invoices   # 30 synthetic invoices + POs
docker compose up --build
```

Open http://localhost:8000, sign in as **Priya (clerk)** or **Arjun (approver)**, and upload files from `data/generated/invoices/`.

On Linux, if the generator can't write to `data/`, run `mkdir -p data && sudo chown 1000 data` first (the container runs as a non-root user).

Things to try:

- Upload `013-po_mismatch-…pdf` and read the checks.
- Upload the same file twice: you land on the existing invoice.
- Upload a `duplicate-…` file after its original: flagged as a duplicate.
- Approve an invoice with errors as Arjun: you have to give a reason, and it's in the audit trail.
- Switch to **Meera (Globex)**: she sees none of Acme's invoices.

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
docker compose run --rm api python -m scripts.run_evals --provider gemini \
  --models gemini-3.5-flash-lite,gemini-3.8-flash --sleep 4
```

Prints field-level accuracy, accuracy per scenario (clean, scanned, …), cost per invoice and latency, and saves results to `data/eval-results/`.

## Tests

```bash
docker compose run --rm api pytest -q
```

Tests use SQLite and the fake provider, so they need no database or key.

## Layout

```
app/
  main.py              routes: upload, review, approve/reject, PO import
  models.py            data model (tenant_id everywhere, money in minor units)
  extraction.py        the background job: call model, validate, check, transition
  extraction_schema.py the contract with the model (schema + prompt, versioned)
  checks.py            deterministic checks: arithmetic, GSTIN, PO match, duplicates
  workflow.py          invoice state machine
  llm/                 provider wrapper: gemini, fake, pricing table
  auth.py              DEV-ONLY login (replaced in Chapter 7)
  storage.py           local disk now, S3 later (same interface)
migrations/            Alembic; the audit log is protected by a DB trigger
scripts/
  generate_invoices.py synthetic GST invoices with ground truth
  run_evals.py         accuracy / cost / latency report
tests/
```

## What v0 deliberately doesn't do

Real authentication, background queue, retries across restarts, email ingestion, ERP integration, payments, agents taking actions, observability beyond logs. Each has a chapter.
