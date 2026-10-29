# Chapter 2: Production readiness

> **In this chapter:** what changes when real people depend on your software. Environments, configuration and secrets, CI, deployments and rollback, database migrations that don't break running code, backups you have actually restored, health checks that don't make outages worse, and container images and local stacks built the way production needs them.
>
> **Part 2, [Staging on AWS with Terraform](02b-staging-on-aws.md),** puts all of it into practice on a real cloud: infrastructure as code, secrets, CI that publishes and deploys, and the problems you'll hit along the way.

---

## 2.1 Why it matters

At the MVP stage the question is "does it work?". From the first real users on, two new questions matter more:

1. **Can we change it safely?** Every deploy is a chance to break something that worked yesterday.
2. **Can we recover when something breaks?** Something will: a bad deploy, a deleted table, a crashed server, a provider outage.

Everything in this chapter serves one of those two. None of it is glamorous, and most of it is invisible when it works. Teams that skip it don't usually fail on day one. They fail on the first bad day, when there's no backup that restores, no way to roll back, and no record of what changed.

A useful test for whether you're ready:

> **Could someone else on the team deploy a change, and undo it, at 3 a.m., without you?**

---

## 2.2 Environments

At minimum, you need three:

| Environment | Purpose | Who depends on it |
|---|---|---|
| **Development** | Your machine; break things freely | Only you |
| **Staging** | A production-like copy for final checks | The team, never customers |
| **Production** | The real thing | Customers |

### Build once, promote the same artifact

The rule that makes environments useful: **the exact build that was tested is the one that ships.** CI builds one container image, tags it with the git commit SHA, and that same image goes to staging and then to production. Nothing is rebuilt between environments; only configuration differs.

If staging and production run different builds, a green staging tells you very little. A dependency may have moved, a base image may have changed, a build flag may differ.

### What should differ between environments, and what shouldn't

- **Should differ:** configuration and secrets (database URLs, API keys, which model, feature flags), scale, and who can access it.
- **Shouldn't differ:** the code, the dependency versions, the database engine and version, the way the app starts.

"Works on staging, breaks in production" usually traces back to something in the second list being different.

### By stage

| Stage | Typical setup |
|---|---|
| MVP | Dev and production; staging optional |
| Early production | Dev, staging, production; staging smaller but structurally identical |
| Growth | Plus a short-lived preview environment per pull request |
| Enterprise | Plus per-region environments, and sometimes per-customer ones |

### By product type

- **AI products** need model configuration per environment: separate API keys, separate spending caps, often a cheaper model in development. Never share a key between staging and production.
- **Internal tools** often need staging inside the company network, because the integrations (directory, ERP, internal APIs) are only reachable there.
- **Consumer apps** lean heavily on feature flags, so new code can ship to production turned off and be enabled gradually.
- **B2B SaaS** with enterprise customers is sometimes asked for a dedicated test environment per customer (Chapter 13).

### A staging URL on the internet is production for attackers

Staging tends to run with relaxed settings: test logins, debug pages, seeded data. If it's reachable from the internet, anyone who finds the URL gets those relaxed settings. The cheap version of protection is a shared password (HTTP Basic auth) or an IP allowlist in front of the whole site, with health-check endpoints exempt. Real authentication replaces it later.

---

## 2.3 Configuration and secrets

The baseline: **configuration comes from environment variables, and nothing secret is in the code or the repository.** On top of that, production needs:

- **A secrets manager.** AWS Secrets Manager or SSM Parameter Store, Google Secret Manager, Vault. The platform injects secrets as environment variables at start-up; the application code doesn't change.
- **Separate secrets per environment.** A leaked development key must not unlock production.
- **Least privilege.** The running app gets only the permissions it needs. For databases, that means two users:
  - an **owner** that can change the schema, used only by migrations;
  - an **app user** that can read and write rows but can't create, alter or drop tables.

  A bug, or an SQL injection, can then damage rows but can't drop a table or disable a trigger.
- **Fail fast on dangerous configuration.** The app should refuse to start with settings that are unsafe where it's running: a development login in production, a fake AI provider in production, local file storage on a platform whose disks vanish on every deploy.
- **When a secret leaks, rotate it.** Removing it from the latest commit doesn't help: it's still in git history, in every clone and fork, and possibly in someone's logs already. Revoke it and issue a new one.

### Stopping secrets before they're committed

Two layers, because either can be skipped:

1. **A pre-commit hook** that scans each commit for things that look like secrets (gitleaks, trufflehog, detect-secrets).
2. **The same scan in CI**, over the whole history, on every push.

Scanners recognise known patterns: AWS key IDs, Google API keys, private keys. They miss secrets that look like random strings without context. They reduce the risk; they don't remove it.

---

## 2.4 Continuous integration

> **Anything you'd check by hand before a release, a machine checks on every change.**

A good CI pipeline for a web application at this stage:

| Check | Catches |
|---|---|
| Lint (with security rules) | Likely bugs, risky patterns, inconsistent style |
| Tests | Regressions in behaviour |
| Migrations on a real database (empty → latest) | Migrations that only work on SQLite, or on your machine |
| Models match migrations | "Changed the model, forgot the migration" |
| Migrations reverse (down, then up) | One-way migrations nobody noticed |
| Secret scan over full history | Credentials in the repository |
| Dependency vulnerability audit | Known CVEs in what you ship |
| Build the production image, start it, call its health check | Images that build but don't run |
| **AI: an eval run when prompts, schemas or model code change** | Prompt edits that quietly break accuracy |

Practical rules:

- **Keep it under ten minutes.** Slower than that and people batch changes or look for ways around it.
- **Run independent checks in parallel.** Lint and tests don't need to wait for the image build.
- **Pin your CI actions to commit SHAs,** not tags. A tag can be moved to point at different code; a commit SHA can't. (More in Chapter 8.)
- **Lock your dependencies.** A requirements file with `>=` installs whatever is newest that day, so two builds of the same commit can differ. Generate a lock file with exact versions and build from it.
- **Gate expensive checks.** A real-model eval costs money and time. Run it when the files that affect model behaviour change, not on every typo fix.

---

## 2.5 Deployment and rollback

### Image tags

Tag images with the commit SHA, never only `latest`. At any moment you should be able to answer "exactly what code is running in production?" by reading one tag, and roll back by redeploying the previous one.

### Deployment strategies

| Strategy | How it works | Downtime | Fits |
|---|---|---|---|
| **Recreate** | Stop old version, start new | Brief | MVP, internal tools with maintenance windows |
| **Rolling** | Replace instances one at a time | None, if health checks are right | Early production (most managed platforms default to this) |
| **Blue-green** | Run both versions; switch traffic at once | None; instant rollback | Growth |
| **Canary** | Send a small share of traffic to the new version, watch error rates, then continue or roll back | None; limits blast radius | Growth and enterprise |

All strategies except recreate have one thing in common: **for a while, the old and new versions run at the same time, against the same database.** That's why migrations need special care.

### Rollback

Rolling back should be one command, practised before you need it. But know its limit: **rolling back the code doesn't roll back the database.** If the new version's migration dropped a column, the old version comes back to a database it can't use. The next section is how to avoid that.

---

## 2.6 Database migrations in production

### The rule

> **Every schema change must work with both the code that's currently running and the code that's about to run.**

### Expand and contract

To make a breaking change, split it across releases. Renaming `vendor_name` to `supplier_name`:

1. **Expand (release 1):** add `supplier_name`. Code writes to both columns, reads from the old one.
2. **Backfill:** copy existing values across, in batches.
3. **Switch (release 2):** code reads from `supplier_name`.
4. **Contract (release 3):** stop writing `vendor_name`, then drop it.

At every step, both the running version and the next one work. A single `RENAME COLUMN` breaks whichever version isn't expecting it.

The same pattern covers making a column required (add nullable → backfill → set NOT NULL later), splitting a table, or changing a type.

### Changes that lock tables

On a busy table, some operations block reads or writes while they run:

| Operation | Risk | Safer approach (Postgres) |
|---|---|---|
| Create index | Blocks writes while building | `CREATE INDEX CONCURRENTLY` |
| Add column with a volatile default, or change type | Rewrites the table | Add nullable, backfill, then constrain |
| Backfill in one statement | Long lock, huge transaction | Batches of a few thousand rows |
| Add a foreign key or check constraint | Scans the table under lock | Add `NOT VALID`, then `VALIDATE` separately |

Set a `lock_timeout` for migrations, so a migration waiting for a lock fails quickly instead of queueing every other query behind it.

### Run migrations as their own step

Don't run migrations when the app starts. With two or more instances starting together, both try to migrate at once. Run them as a separate one-off task in the deploy pipeline, **before** the new app version starts. Add a database lock so that two migration runs, from overlapping deploys, can't interleave.

> **A lock can create the problem it was meant to solve.** In Postgres, `CREATE INDEX CONCURRENTLY` waits for every open transaction in the database to finish. A second migration run *blocked waiting* for the migration lock holds an open transaction. Result: each waits for the other, and Postgres kills one with "deadlock detected". The fix is to poll for the lock (`pg_try_advisory_lock`, committing between attempts) rather than block. This happened while building this chapter's demo; a test that runs two migrations at once caught it.

---

## 2.7 Backups and recovery

### Two numbers

- **RPO (recovery point objective):** how much data you can afford to lose. Nightly backups mean up to 24 hours. Point-in-time recovery (continuous log archiving, standard on managed databases) brings it down to minutes.
- **RTO (recovery time objective):** how long you can afford to be down while restoring.

Enterprise customers will ask for both, in writing (Chapter 13). Decide them deliberately before someone asks.

### A backup you've never restored is a hope

Backups fail silently: wrong permissions, a misconfigured schedule, a dump that's missing a schema, an encryption key nobody kept. The only proof is a restore. A **restore drill**:

1. Restore the latest backup into a new, separate database.
2. Verify it: row counts, schema version, critical constraints and triggers, a few real queries.
3. Time it. Restore time, plus how long it takes someone to notice and start, is your real RTO.
4. Do it on a schedule (monthly is a reasonable start), and after any change to how backups are taken.

### Back up everything the data depends on

The database is rarely the whole picture. Uploaded files, object storage, encryption keys, and configuration all need their own backup and their own place in the drill. A database whose rows point to files that no longer exist is not a working restore.

For object storage (S3 and similar): turn on **versioning** so deletes and overwrites are recoverable, and consider replication to another region for disaster recovery.

### Protect backups from the thing you're protecting against

- Keep backups in a separate account or with separate permissions, so a compromised app or a mistyped command can't delete both the data and its backups.
- Turn on deletion protection for production databases.
- For high-value data, use immutable (write-once) backup storage.

### By stage

| Stage | Database | Files | Drill |
|---|---|---|---|
| MVP | Managed DB with automated daily backups | Versioned bucket | Once, by hand |
| Early production | Point-in-time recovery, 7–35 days retention | Versioning + lifecycle rules | Scripted, monthly |
| Growth | Plus cross-region copies | Plus cross-region replication | Automated, alerts on failure |
| Enterprise | Plus separate backup account, immutable copies, contractual RPO/RTO | Same | Audited, evidence kept for compliance |

---

## 2.8 Health checks, shutdowns and interrupted work

### Liveness versus readiness

Platforms use health checks to decide what to do with an instance. There are two different questions, and mixing them up turns small outages into big ones:

| Check | Question | If it fails, the platform… | Should check |
|---|---|---|---|
| **Liveness** | Is this process able to respond at all? | **Restarts** the instance | Almost nothing: the process answers |
| **Readiness** | Should this instance receive traffic right now? | **Stops sending traffic**, without killing it | Dependencies: database reachable, warmed up |

If liveness checks the database, a two-minute database blip makes the platform restart every healthy instance at once. When the database comes back, every instance is cold-starting together. Keep liveness trivial.

### Graceful shutdown

On every deploy, the platform sends running instances a stop signal, waits a while (30 seconds by default on AWS ECS), then kills them. The app should stop accepting new requests and finish in-flight ones within that window.

### Work that outlives the request

Background work that runs inside the web process dies with it. On a deploy, a crash or an out-of-memory kill, anything in progress simply stops, often leaving records stuck in an "in progress" state forever.

The cheap version, before a job queue (Chapter 6):

1. Record **when** a record entered an in-progress state.
2. A recovery sweep finds records in progress for longer than any job could take, marks them failed, and retries them. Run it at start-up and on a schedule.
3. Make the job **lock its record** while it runs, so a retry and the original can't both process it.

> **Anything that can start must have a way to be noticed if it never finishes.**

---

## 2.9 Containers done properly

The container image is the unit you build once and promote through every environment (2.2). That only works if the image itself is built for production. A Dockerfile that runs on your laptop usually isn't: it's large, runs as root, contains your test tools, sometimes contains secrets, and ignores the platform's stop signal.

This section is the practical checklist, then a worked example you can copy, then the commands to check it.

### What a production image needs

| Practice | Why | How |
|---|---|---|
| **Pin the base image** | `python:latest` changes under you; so do the system tools inside it | Name the exact variant (`python:3.12-slim-trixie`). For full reproducibility, pin the digest (`@sha256:…`) |
| **Install from a lock file** | `>=` ranges install whatever is newest that day | Exact versions in `requirements.txt` (or `poetry.lock`, `uv.lock`, `package-lock.json`) |
| **Copy dependency files before code** | Docker caches each layer; code changes then don't reinstall every dependency | `COPY requirements.txt` → `RUN pip install` → `COPY app` |
| **Separate build targets** | Test tools, compilers and generators in production are attack surface and bloat | Multi-stage Dockerfile: a `runtime` target that ships, a `dev` target for local work |
| **Run as a non-root user** | If the app is compromised, root inside the container makes escaping and tampering easier | `RUN useradd …` then `USER appuser` |
| **No secrets in the image** | Anyone who can pull the image can read every layer, including `ENV` and `ARG` values (`docker history`) | Secrets arrive as environment variables at run time. `.env` is listed in `.dockerignore` |
| **A `.dockerignore`** | Without it, `COPY . .` sends your `.env`, `.git` and local data into the build | Exclude secrets, caches, generated data and VCS folders |
| **Exec-form `CMD`** | Shell form (`CMD uvicorn …`) runs under `/bin/sh`, which doesn't pass the stop signal on; the app never shuts down gracefully and gets killed | `CMD ["uvicorn", "app.main:app", …]` |
| **Graceful shutdown shorter than the platform's timeout** | The platform waits a fixed time (30 s on ECS by default) and then kills | Set the server's graceful timeout a little below it (`--timeout-graceful-shutdown 25`) |
| **Logs to stdout, unbuffered** | Platforms collect stdout; buffered output is lost when a container dies | `PYTHONUNBUFFERED=1`; never log to files inside the container |
| **Configuration from the environment** | One image for every environment | No environment names or URLs baked in |
| **Know what's running** | "Which version is in production?" should have a one-word answer | Pass the commit SHA as a build argument and expose it |
| **Trust the proxy only where there is one** | Behind a load balancer, the client's IP and scheme arrive in headers | Enable proxy-header handling in the server for deployed images |

Two habits that catch most mistakes:

1. **Test the shipped image, not just the code.** In CI, build the runtime target, start it, and call its liveness endpoint. Also assert that things which shouldn't be there aren't (for example, that `pytest` can't be imported).
2. **Keep development conveniences out of the runtime target.** Auto-reload, bind mounts, seed data and debug tools belong in the `dev` target and in Compose, never in what ships.

### Worked example: Ledgerline's Dockerfile

One file, two images:

```dockerfile
# Pinned base. Debian 13's postgresql-client is v17, new enough to back up
# a Postgres 16 server (pg_dump must be at least the server's version).
FROM python:3.12-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 1000 appuser

# ---- runtime: what ships to staging and production
FROM base AS runtime
# Dependencies before code, so code changes reuse the cached install layer.
COPY requirements.txt .
# Exact, locked versions.
RUN pip install -r requirements.txt
# Only what the app needs at run time.
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
# "Which version is running?" has an answer.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
# Never run as root.
USER appuser
# Exec form, so the stop signal reaches uvicorn; graceful timeout below ECS's 30 s.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "25"]

# ---- dev: runtime + test tools, generator and pg_dump; used by docker compose
FROM base AS dev
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY . .
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Build them with:

```bash
docker build --target runtime --build-arg GIT_SHA=$(git rev-parse HEAD) -t ledgerline:$(git rev-parse --short HEAD) .
docker build --target dev -t ledgerline:dev .
```

### The local stack with Compose

Compose describes how the pieces start together. Use it to rehearse the production deploy order, not to hide it.

**Principles:**

- **Wait for health, not for start.** "The database container started" doesn't mean "the database accepts connections". Give stateful services a health check, and make dependants wait on `service_healthy`.
- **Model one-off jobs as one-off services.** Migrations run once, exit, and the app waits on `service_completed_successfully`, exactly as a deploy pipeline runs a migration task before starting the new version.
- **Same image, different credentials.** The migration job and the app run the same image; the job connects as the schema owner, the app as the restricted user.
- **Know which volumes hold state.** `docker compose down` keeps named volumes (your database); `docker compose down -v` deletes them.
- **Ports are `host:container`.** To move the app to another port on your machine, change only the host side (`"8007:8000"`). The container keeps listening where the image and CI expect.

> **Worked example: Ledgerline's local stack**
>
> ```
> db (Postgres 16) ──healthy──▶ migrate (one-off) ──exit 0──▶ api
>                                │                              │
>                                ├ alembic upgrade head         ├ connects as ledgerline_app
>                                │   (as ledgerline, the owner) │   (rows only)
>                                ├ python -m app.db_roles       ├ startup sweep: recover
>                                │   (create ledgerline_app)    │   interrupted extractions
>                                └ python -m app.seed           └ uvicorn with --reload
>                                    (demo data; dev only)
> ```
>
> ```yaml
> services:
>   db:
>     image: postgres:16
>     healthcheck:
>       test: ["CMD-SHELL", "pg_isready -U ledgerline"]
>   migrate:
>     build: { context: ., target: dev }
>     depends_on: { db: { condition: service_healthy } }
>     command: sh -c "alembic upgrade head && python -m app.db_roles && python -m app.seed"
>     restart: "no"
>   api:
>     build: { context: ., target: dev }
>     ports: ["8000:8000"]
>     depends_on: { migrate: { condition: service_completed_successfully } }
> ```
>
> The full file is `ledgerline/docker-compose.yml`. In Chapter 1, the API ran `alembic upgrade head` itself on every start; with two instances starting together, both would try to migrate (exercise 2.11, question 2).

### Checking the stack

Run these from the folder that contains `docker-compose.yml`. Replace `8000` with your host port if you changed it.

| What you're checking | bash / zsh | PowerShell | Expect |
|---|---|---|---|
| Start-up order | `docker compose ps -a` | same | `migrate` exited 0; `db` and `api` running |
| Why something stopped | `docker compose logs api --tail 30` | same | Start-up messages, or the error |
| Liveness | `curl -s localhost:8000/livez` | `curl.exe -s http://localhost:8000/livez` | `{"status":"ok"}` |
| Readiness | `curl -s localhost:8000/readyz` | `curl.exe -s http://localhost:8000/readyz` | ok, database ok |
| Liveness vs readiness | `docker compose stop db`, then both checks, then `docker compose start db` | same | `livez` ok, `readyz` 503 while the database is down |
| Schema version | `docker compose exec -T db psql -U ledgerline -d ledgerline -tAc "select version_num from alembic_version"` | same | The latest migration |
| Restricted app user | `… -tAc "select rolname from pg_roles where rolname='ledgerline_app'"` | same | `ledgerline_app` |
| Tests | `docker compose run --rm --no-deps api pytest -q \| tail -1` | `… 2>&1 \| Select-Object -Last 1` | All passed |
| Backup restores | `docker compose run --rm api python -m scripts.backup_drill \| tail -1` | `… 2>&1 \| Select-Object -Last 1` | `RESULT: PASS` |
| Rebuild after dependency changes | `docker compose up --build` | same | New image used |
| Start clean (deletes local data) | `docker compose down -v` | same | Volumes removed |

### Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| A service shows `exited 255` after a restart of Docker Desktop | The engine stopped it | Check `logs`; `docker compose up -d` |
| Old behaviour after changing code or requirements | Compose reused the cached image | `docker compose up --build` |
| `port is already allocated` | Something else uses the host port | Change the host side of the mapping |
| `Permission denied` writing to a bind-mounted folder (Linux) | Container user (UID 1000) doesn't own it | `sudo chown 1000 data` |
| `Found orphan containers` | A service was renamed or removed | `docker compose up --remove-orphans` |
| `database system was not properly shut down` | Postgres was killed rather than stopped | Recovers automatically; stop with `docker compose down` or Ctrl+C |
| `pg_dump: server version mismatch` | Client tools older than the server | Use a client at least as new as the server |
| `head` not found, or `curl` asks for `Uri:` (Windows) | PowerShell aliases `curl` to `Invoke-WebRequest` and has no `head` | Use `curl.exe` and `Select-Object -Last N` |

### By stage and product type

- **MVP:** one Dockerfile and a Compose file. The points in the table above are cheap from day one; retrofitting non-root or secret-free images later is harder.
- **Growth:** smaller images (distroless or minimal bases), image vulnerability scanning in CI, a build cache in CI.
- **Enterprise, and anything self-hosted by customers:** the image becomes a product. Customers will ask for signed images, a software bill of materials (SBOM), a vulnerability policy and supported base images (Chapters 8 and 13).
- **Internal tools:** often must use the company's approved base images and private registry; find out before you build.

---

## 2.10 Challenges and watch-outs

- **Staging drift.** Staging slowly diverges from production (different versions, manual fixes, missing settings) until it stops predicting anything. Build both from the same code and configuration templates.
- **"We have backups."** Until restored, you have files. Drill.
- **Migrations tested only on an empty database.** The slow, locking migration only shows up with real volumes. Know your largest tables, and test risky migrations against a copy of production-sized data.
- **CI that people stop trusting.** Flaky tests teach everyone to re-run until green, and real failures get re-run too. Fix or quarantine flaky tests quickly.
- **Health checks that depend on everything.** See 2.8.
- **Secrets in CI logs.** Commands that print their environment, debug output, error messages that include connection strings. Most CI systems mask registered secrets, but only exact matches.
- **The free-tier clock.** New cloud accounts often come with credits that expire. Infrastructure left running burns them; tear down what you aren't using, and make it easy to recreate (infrastructure as code, Chapter 4).

**Signals you need more than this chapter:**

- Deploys are frequent enough that rolling deploys feel risky → blue-green or canary.
- Background work is regularly interrupted, or needs retries and scheduling → a job queue (Chapter 6).
- You can't tell from the outside whether production is healthy → observability (Chapter 3).

---

## 2.11 Exercise: find the production bugs

Here's an app that works perfectly on a laptop. Find what breaks in production.

It's a web app that accepts uploads and processes each one with a background task inside the web process. The task first sets the record's status to `processing`, then calls a slow external API, then sets it to `done`. The container starts with `migrate-database && start-web-server`. Its health check endpoint runs `SELECT 1` against the database, and the platform restarts any instance whose health check fails.

1. A user uploads a file. Two seconds later, a deploy replaces the container. What happens to the upload?
2. Production runs two instances for reliability. A deploy starts both at the same moment. What can go wrong?
3. The database has a two-minute outage. What does the platform do to the app, and what happens when the database comes back?

<details>
<summary><strong>Answers</strong></summary>

1. **The record is stuck in `processing` forever.** The deploy killed the process mid-task; nothing will ever finish it or mark it failed, and nobody is told. Fix: record when processing started, sweep for records stuck longer than any task could take, mark them failed and retry (2.8). Long term: a job queue that survives restarts (Chapter 6).

2. **Both instances run migrations at once.** One may fail with "already exists" errors and crash on start-up; at worst a migration half-applies. Fix: run migrations as a separate deploy step before the app starts, and take a database lock during migrations (2.6).

3. **The platform restarts every instance, repeatedly, for two minutes.** The health check fails because the database is down, so the platform concludes the app is broken. When the database returns, every instance is cold-starting at once. Fix: split liveness (process responds, no dependencies) from readiness (dependencies reachable) (2.8).

</details>

---

> ### Worked example: Ledgerline v0.2
>
> Ledgerline v0.1 had all three bugs from the exercise. Here's what changed to make it production-ready.
>
> | Problem | Change |
> |---|---|
> | Extraction interrupted by a deploy leaves invoices stuck in `extracting` | `status_changed_at` column; a recovery sweep at start-up (and as a command) fails and retries anything stuck longer than 10 minutes; the extraction job locks the invoice row so two runs can't process it |
> | Migrations ran on app start | A separate `migrate` step; the app never migrates. Migrations take a Postgres advisory lock, polled to avoid the deadlock described in 2.6 |
> | `/healthz` checked the database | `/livez` (process only) and `/readyz` (database reachable) |
> | App connected as the schema owner | `ledgerline_app` user: read/write rows only; can't drop tables, can't update or delete audit events. Migrations use a separate URL |
> | Files on local disk | An S3 storage backend behind the same interface; the app refuses local storage when deployed |
> | Dev login could be enabled anywhere | Refused in production; on staging only behind a shared password gate |
> | `requirements.txt` with `>=` ranges | Exact lock files for runtime and development; the production image contains only runtime dependencies |
> | No CI | Lint, tests, migrations on real Postgres (upgrade, drift check, reversal, concurrent runs), secret scan, dependency audit, eval harness, production image build and start-up check |
> | No backup verification | A drill script: dump, restore into a scratch database, verify row counts, schema version, audit trigger and every referenced file, and report the time taken |
>
> **The first real migration** added `status_changed_at` the safe way: nullable column (instant), batched backfill, index built `CONCURRENTLY`. It's the expand half of expand-and-contract; making the column `NOT NULL` would be a later, separate migration.
>
> **Things the checks caught while building it:**
>
> - The new index existed in the migration but not in the model. The "models match migrations" check failed until it was declared in both.
> - Two concurrent migration runs **without** the lock: one crashed with a duplicate-key error. **With** a blocking lock: Postgres detected a deadlock (2.6). With a polled lock: both succeeded, one waiting for the other.
> - The backup drill passed when everything was intact and failed when a single uploaded file referenced by the database was removed.
> - A secret scanner flagged a realistic-looking Google API key but not a bare AWS secret key without its key ID: scanners catch patterns, not everything.

---

## 2.12 Cost

At this stage the new costs are CI minutes and a staging environment.

**CI.** On GitHub's Free plan, private repositories include 2,000 Actions minutes a month; public repositories don't use the quota on standard runners. A pipeline like Ledgerline's takes a few minutes per run, so a solo developer or a small team rarely exceeds it. GitHub changed Actions pricing in January 2026 (cheaper hosted runners, a new charge for self-hosted runners), so check the current terms before planning around them.

**Staging.** An always-on staging environment is a real monthly cost. A rough estimate for a small containerised app on AWS, using published us-east-1 on-demand rates (other regions differ; check before relying on it):

| Item | Approx. monthly cost, running 24/7 |
|---|---|
| One small container (0.25 vCPU, 0.5 GB) on Fargate | ~$9 |
| Application Load Balancer (base + light traffic) | ~$17–22 |
| Smallest managed Postgres (db.t4g.micro, single-AZ) + 20 GB storage | ~$14 |
| Public IPv4 addresses (load balancer and task) | ~$11 |
| Container registry, object storage, logs, parameters | ~$1–3 |
| **Total** | **~$50–60** |

Things that change the number a lot:

- **NAT gateways** cost about $33 per month per availability zone before data charges, often more than everything else combined. Small environments can avoid them by giving containers public IPs (restricted by security groups), at some cost in isolation.
- **Turning staging off** when nobody is using it. Containers can scale to zero; a stopped database still charges for storage, and a load balancer charges while it exists. Infrastructure as code makes deleting and recreating the whole environment routine ([part 2](02b-staging-on-aws.md#2b11-day-to-day-operations)).
- A **single small VM running the whole stack** costs roughly a third as much, at the price of doing your own database backups and having no rolling deploys. A legitimate MVP choice; Chapter 4 compares the options.

---

## 2.13 Demo

The reference implementation is in [`ledgerline/`](../ledgerline/README.md).

```bash
cd ledgerline
cp .env.example .env && mkdir -p data
docker compose run --rm --no-deps api python -m scripts.generate_invoices
docker compose up --build        # db → migrate (one-off) → api
```

Then:

1. Watch the start-up order: the API waits for the `migrate` job to finish.
2. Stop the database (`docker compose stop db`) and compare `/livez` with `/readyz`.
3. Run the backup drill: `docker compose run --rm api python -m scripts.backup_drill`.
4. Install the hooks (`pre-commit install` at the repository root) and try committing a file containing a fake API key.
5. Push a branch to GitHub and read the CI run.

**Next in this chapter:** [part 2, Staging on AWS with Terraform](02b-staging-on-aws.md), which deploys the same image to a real staging environment: migrations as a one-off task, secrets from Parameter Store, files in S3, and CI that tests, publishes and deploys.

---

## 2.14 Checklist

You're production-ready at this stage when:

- [ ] There's a staging environment built the same way as production, and it isn't publicly usable without a password
- [ ] The image that's tested is the image that ships, tagged with the commit SHA
- [ ] Dependencies are locked to exact versions
- [ ] Secrets live in a secrets manager, differ per environment, and are scanned for in commits and CI
- [ ] The app uses a database user that can't change the schema
- [ ] The app refuses to start with configuration that is dangerous where it's running
- [ ] CI runs lint, tests, migrations on a real database, a secret scan, a dependency audit and an image build on every change
- [ ] For AI products: an eval runs automatically when prompts, schemas or model code change
- [ ] Migrations run as a separate step, are locked against concurrent runs, and follow expand-and-contract for breaking changes
- [ ] Rolling back is one command, and you've done it once
- [ ] RPO and RTO are written down, and a restore drill has met them
- [ ] Uploaded files and other non-database data are backed up and included in the drill
- [ ] Liveness and readiness are separate checks
- [ ] The shipped image is a separate target: pinned base, locked dependencies, non-root user, exec-form command, no secrets, no test tools
- [ ] The local stack starts in the same order as a deploy: database healthy, migrations, then the app
- [ ] Interrupted background work is detected and recovered

For the cloud deployment itself, see [the checklist in part 2](02b-staging-on-aws.md#2b15-checklist).

---

**Next:** [Chapter 3 — Observability](03-observability.md): knowing what your system is doing, and finding out it's broken before your customers tell you.
