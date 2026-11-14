# Chapter 5, part 2: The data layer in practice

> **In this part:** two demos. First, a million synthetic invoices in local Postgres show which of Ledgerline's queries break at volume, how to find them, and which ones an index can fix. Then a restore drill on AWS: damage the staging database on purpose, restore it to the minute before, and prove the copy is right. For each: the steps, the real results, and what went wrong. Concepts are in [part 1](05-data-layer.md).

Both were run in November 2026, on a Windows laptop (PowerShell 5, Docker Desktop) and in the `ap-southeast-2` (Sydney) region. The problems described really happened.

---

## Contents

- [5B.1 Demo 1: from slow query to index](#5b1-demo-1-from-slow-query-to-index)
- [5B.2 What demo 1 showed](#5b2-what-demo-1-showed)
- [5B.3 Demo 2: a restore drill on RDS](#5b3-demo-2-a-restore-drill-on-rds)
- [5B.4 What demo 2 showed](#5b4-what-demo-2-showed)
- [5B.5 Troubleshooting](#5b5-troubleshooting)
- [5B.6 Checklist](#5b6-checklist)

---

## 5B.1 Demo 1: from slow query to index

**Goal:** see what a million invoices do to the invoice list, find the slow queries with the database's own tools, fix what an index can fix, and recognise what it can't.

**Cost:** free (local Docker). About 30 minutes, and 1–2 GB of disk while the data exists.

### The pieces

| File | What it does |
|---|---|
| [`app/queries.py`](../ledgerline/app/queries.py) | The invoice list's queries, moved out of the route handler into named functions: the page of newest invoices, the status counts (the tabs), the LLM spend total, the duplicate-check base query. The app and the measuring script use the same functions, so what's measured is exactly what runs. |
| [`scripts/load_volume.py`](../ledgerline/scripts/load_volume.py) | Fills the development database with synthetic invoices, documents and extraction runs, generated inside Postgres with `generate_series()` (about a minute for a million; inserting through the ORM would take hours). Skewed like real B2B data: one tenant owns 70% of the rows. Refuses to run outside development. |
| [`scripts/explain_queries.py`](../ledgerline/scripts/explain_queries.py) | Runs each query in `app/queries.py` with `EXPLAIN (ANALYZE, BUFFERS)` for the largest tenant and prints time and access method; then the top statements from `pg_stat_statements`. `--plans` prints full plans; `--reset` clears the statistics. |
| [`migrations/versions/0003_indexes_for_volume.py`](../ledgerline/migrations/versions/0003_indexes_for_volume.py) | The fix: four indexes shaped like the queries, built `CONCURRENTLY`. |
| `docker-compose.yml` | Starts Postgres with `shared_preload_libraries=pg_stat_statements`. |

### Steps

From the `ledgerline` folder:

```powershell
# 1. Recreate the stack: the db service's command changed, and the image has new code
docker compose up -d --build --force-recreate db migrate api

# 2. Go back to before the fix (migrate has already applied 0003), then load the volume
docker compose run --rm migrate alembic downgrade 0002
docker compose run --rm api python -m scripts.load_volume --invoices 1000000

# 3. Measure without the indexes. Run it twice: the first run warms the cache
docker compose run --rm api python -m scripts.explain_queries
docker compose run --rm api python -m scripts.explain_queries

# 4. Apply the fix, and measure again
docker compose run --rm migrate alembic upgrade head
docker compose run --rm api python -m scripts.explain_queries

# 5. What real use costs: clear the statistics, click around the invoice list
#    (http://localhost:8000, or your port) and its status tabs, then look
docker compose run --rm api python -m scripts.explain_queries --reset
docker compose run --rm api python -m scripts.explain_queries

# 6. Done: remove the million rows (the next `up` re-creates the normal demo data)
docker compose down -v
```

### Results

For the largest tenant (700,000 invoices), warm cache, on one laptop:

| Query | Before (no new indexes) | After `0003` | Access after |
|---|---|---|---|
| List page: newest 200 | 204 ms | **1.1 ms** | Index scan backward on `(tenant_id, created_at)` |
| List page: newest 200 needing review | 126 ms | **1.1 ms** | Index scan backward on `(tenant_id, status, created_at)` |
| Duplicate check: same vendor, number | 0.0 ms | 0.0 ms | The existing `invoice_number` index |
| Duplicate check: same vendor, amount, date | 85 ms | **4.9 ms** | Index on `(tenant_id, vendor_tax_id)` |
| Status counts (the tabs) | 108 ms | 128 ms | Still one index entry per invoice |
| Total LLM spend | 97 ms | 104 ms | Still one index entry per invoice |

On the second machine (after the indexes), the list pages took 2–3 ms and the duplicate checks 0.1 ms, while status counts took **358–812 ms** and the spend total **205–328 ms**. Same data, same indexes, different hardware and cache state: measure more than once, and on something like production, before trusting a number.

Building the four indexes took **4–8 seconds** on a million invoices. Afterwards, the `invoices` table was 239 MB and its indexes **377 MB**.

### Reading the "before" plans

Before `0003`, the list page used the single-column `tenant_id` index: it found all 700,000 of the tenant's rows, read them, sorted them by `created_at` and kept 200. With `(tenant_id, created_at)`, Postgres walks the index backwards from the tenant's newest entry and stops after 200 rows. Nothing to sort, almost nothing to read. That's "equality columns first, then the sort column" (part 1, 5.5) in one plan.

The filtered list ("needing review") combined two single-column indexes (`status` and `tenant_id`) with a bitmap scan before the fix. The three-column index serves it directly.

### What `pg_stat_statements` said

**After the bulk load**, the top statements were the load itself (`INSERT … SELECT` into each table, 12–85 seconds each), and one line that's easy to miss:

```
950000 calls   13287 ms total   0.0 ms avg   SELECT $2 FROM ONLY "public"."documents" x WHERE "id" …
```

That's Postgres checking the foreign key from each new invoice to its document, once per row. Foreign keys cost something on every insert, and the checks need an index on the referenced side (here, the primary key).

**After clicking around the app** (with `--reset` first), the ranking by total time was:

```
4 calls   536 ms total   134 ms avg   SELECT invoices.status, count(*) … GROUP BY invoices.status
4 calls   249 ms total    62 ms avg   SELECT coalesce(sum(extraction_runs.cost_usd), 0) …
6 calls    30 ms total     5 ms avg   SELECT invoices.id, invoices.tenant_id, … (the list page)
```

The page's own list query was now the cheap part; the counts and the total above it were the expensive part. That's the ranking to act on next.

The same output also showed a line nobody expected:

```
1 calls   5 ms total   ALTER ROLE ledgerline_app PASSWORD 'app-dev-pw'
```

`app.db_roles` sets the app user's password with a statement that includes it, and `pg_stat_statements` stored the text as written. Here it's the development default; in production, the same view would show the real password to anyone allowed to read it (part 1, 5.4).

---

## 5B.2 What demo 1 showed

1. **Scale changes which queries matter.** At 30 invoices, every query took a few milliseconds. At 700,000, the list page took 200 ms with the "obvious" single-column indexes that were already there.
2. **The fix comes from measuring, not guessing.** The plans said exactly what was wrong (find everything, sort, keep 200), and the shape of the fix followed from the query: equality columns first, then the sort column.
3. **Indexes speed finding, not counting.** The list got 200 times faster; the counts above it didn't change. That needs a design change (part 1, 5.5), and Ledgerline will make it when the numbers justify it.
4. **Index changes are migrations like any other.** Built `CONCURRENTLY` (no write lock), tested with two migration runs at once in CI (Chapter 2's deadlock lesson), and the now-redundant single-column indexes left in place for a later "contract" step.
5. **Plans aren't fixed.** The covering index for the spend total was used on one machine and not the other, depending on vacuum state and statistics.

### What went wrong

> **A 4 GB Docker build.** The first `docker compose up --build` after Chapter 4 sat on `COPY . .` and then on `chown` for **35 minutes**, then Docker Desktop's engine stopped responding. The build log said why: `transferring context: 4.31GB`. Chapter 4's Terraform stacks had each downloaded their provider plugins into `infra/*/.terraform` (hundreds of MB per stack), and `.dockerignore` didn't exclude them, so every byte was sent to Docker and copied into the development image.
>
> The fix was a few lines in `.dockerignore` (`infra`, `build`, `deploy`, `.venv`, `.git`, `*.zip`), then `docker builder prune -f` to drop the half-built layers. Docker Desktop itself had to be force-closed (stop its processes, `wsl --shutdown`, start it again). Lessons:
> - **The build context is everything in the folder that isn't ignored.** Check the `transferring context` line in the build output; it should be megabytes, not gigabytes.
> - New tools leave new folders behind. Review `.dockerignore` (and `.gitignore`) when you add one.

> **The "before" measurement got skipped.** The migration service applies every migration when the stack starts, so the first measurement after `up` was already "after". To see "before", downgrade to `0002` first (step 2 above). An improvement you can't compare with a baseline isn't measured.

---

## 5B.3 Demo 2: a restore drill on RDS

**Goal:** prove that staging's backups can bring back data as it was before a mistake, and measure how long it takes.

**Cost:** cents. A second `db.t4g.micro` for under an hour per restore, plus a few one-off ECS tasks.

### The scenario

A script meant to reject *one* invoice lost its `AND id = …` in a refactor, and rejected all of one customer's invoices. Multi-AZ won't help: the standby received the same `UPDATE` within milliseconds. Only point-in-time recovery can.

### The pieces

| File | What it does |
|---|---|
| [`app/drill.py`](../ledgerline/app/drill.py) | `prepare` creates a "Drill Co" tenant with 500 invoices (350 approved, 150 needing review). `state` prints a **safe restore point** (the database's clock, rounded up to the next second), the count per status and a **fingerprint**: an MD5 over every Drill Co invoice's id, status and amount. `accident` sets all of them to `rejected`. Runs as the app's database user, touches only Drill Co, and refuses to run in production. |
| [`scripts/drill.ps1`](../ledgerline/scripts/drill.ps1) | Runs `python -m app.drill <command>` as a one-off ECS task (the database has no public address, so the drill must run inside the VPC), waits for it and prints its output. `-DbHost` points it at a different database, such as the restored copy. |

`drill.ps1` uses the same pattern as CI's migration step (Chapter 2): the migration task definition, with the command overridden, and the subnets and security group read from the running service. It writes the network and override JSON to files rather than passing them inline, because PowerShell 5 mangles quotes in arguments to native programs, and with `[IO.File]::WriteAllText`, because the AWS CLI rejects JSON files that start with a byte-order mark. On macOS or Linux, run it with PowerShell 7 (`pwsh`), or translate the same `aws ecs run-task` call to a shell script.

### Steps

**Part A: make sure staging is running the new code** (from the `ledgerline` folder):

```powershell
$Env:AWS_PROFILE = "ledgerline"     # after `aws login --profile ledgerline`
aws rds describe-db-instances --db-instance-identifier ledgerline-staging --query "DBInstances[0].DBInstanceStatus" --output text
# if it says "stopped":
aws rds start-db-instance --db-instance-identifier ledgerline-staging --query "DBInstance.DBInstanceStatus" --output text
aws rds wait db-instance-available --db-instance-identifier ledgerline-staging
```

Push the commit that contains `app/drill.py`, and wait for the pipeline's **deploy-staging** job to finish: it builds the image, runs migrations (including `0003`) and deploys. The drill runs from that image, so until then `python -m app.drill` doesn't exist in AWS.

**Part B: the drill**

```powershell
Set-ExecutionPolicy -Scope Process Bypass    # if PowerShell refuses to run scripts

# 1. Known good state. Write down the safe restore point and the fingerprint.
.\scripts\drill.ps1 prepare

# 2. A minute or two later, the accident; then confirm the damage
.\scripts\drill.ps1 accident
.\scripts\drill.ps1 state

# 3. Wait until the backups cover the good state. Repeat until this is LATER than the
#    safe restore point from step 1 (the log is shipped about every five minutes)
aws rds describe-db-instances --db-instance-identifier ledgerline-staging --query "DBInstances[0].LatestRestorableTime" --output text

# 4. Restore into a NEW instance, with the same network settings as the original
$before = "2026-11-14T06:35:45Z"    # the safe restore point from step 1, or later, but before the accident
$sg = aws rds describe-db-instances --db-instance-identifier ledgerline-staging --query "DBInstances[0].VpcSecurityGroups[0].VpcSecurityGroupId" --output text
$start = Get-Date
aws rds restore-db-instance-to-point-in-time `
  --source-db-instance-identifier ledgerline-staging `
  --target-db-instance-identifier ledgerline-staging-restore `
  --restore-time $before `
  --db-subnet-group-name ledgerline-staging --vpc-security-group-ids $sg `
  --no-publicly-accessible --no-multi-az --db-instance-class db.t4g.micro --storage-type gp2 `
  --backup-retention-period 0 --no-deletion-protection `
  --query "DBInstance.DBInstanceStatus" --output text
aws rds wait db-instance-available --db-instance-identifier ledgerline-staging-restore   # re-run if it times out
"Restore took {0:N0} minutes" -f ((Get-Date) - $start).TotalMinutes

# 5. Verify the copy: same counts, same fingerprint as step 1
$restored = aws rds describe-db-instances --db-instance-identifier ledgerline-staging-restore --query "DBInstances[0].Endpoint.Address" --output text
.\scripts\drill.ps1 state -DbHost $restored
```

The restore flags, and why:

| Flag | Why |
|---|---|
| `--db-subnet-group-name`, `--vpc-security-group-ids` | Without them, the copy goes into the default subnet group with the default security group, and the app's tasks can't reach it |
| `--no-publicly-accessible` | Same rule as the original: no public address, ever |
| `--db-instance-class`, `--storage-type gp2` | Same size and type as the source; also what the Free plan allows |
| `--backup-retention-period 0` | A short-lived copy needs no backups of its own |
| `--no-deletion-protection` | So it can be deleted straight after the drill |

**Part C: clean up**

```powershell
aws rds delete-db-instance --db-instance-identifier ledgerline-staging-restore --skip-final-snapshot --delete-automated-backups --query "DBInstance.DBInstanceStatus" --output text

# Pause staging again (section 3B.13 has the full version, with alarms and the canary)
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 0 --query "service.desiredCount"
aws rds stop-db-instance --db-instance-identifier ledgerline-staging --query "DBInstance.DBInstanceStatus" --output text
```

### Results

| | |
|---|---|
| Good state recorded (`prepare`) | 06:35:12 UTC: 350 approved, 150 needs_review, fingerprint `3067b163…48ee1` |
| Accident | 06:36:43 UTC: 500 invoices set to `rejected`, fingerprint `ca4695ac…52015` |
| Latest restorable time passed the good state | about 2 minutes after the accident (06:37:33) |
| Restore, from command to "available" | **10–11 minutes**, on each of three attempts |
| Verification task | about 1 minute |
| Restored copy (third attempt, restored to 06:35:45) | 350 approved, 150 needs_review, fingerprint `3067b163…48ee1`: **identical** |
| Attempts needed | 3 |

---

## 5B.4 What demo 2 showed

The backups work, and they can bring back one customer's data as it was before a mistake. Getting there took three restores and most of an afternoon, and the two failures taught more than the success.

### Attempt 1: the right time, the wrong data

The first restore targeted 06:35:12, the time `prepare` printed after creating the invoices. It finished in 10 minutes, the verification task connected to it, and Drill Co **wasn't there**.

The time had been printed rounded **down** to the second. The invoices were committed at 06:35:12 *and a fraction*; restoring to exactly 06:35:12 stopped a fraction of a second before them. The restore did exactly what it was asked.

Lessons:
- **Restore to a point safely after the last good change and before the mistake**, not to the exact second of either. Seconds of margin on each side cost nothing.
- **Verify against something recorded beforehand.** Without the fingerprint, an empty Drill Co could have looked like "the restore worked, the data must have been deleted earlier".
- The drill script now prints a **safe restore point**, rounded up, with a reminder.

### Attempt 2: no room for a second database

Retrying while the first copy was still being deleted failed immediately:

```
InstanceQuotaExceeded: You reached the maximum number of instances available with free plan accounts.
```

A restore needs space for one more database, and a copy that's being deleted still counts. The fix was to wait (`aws rds wait db-instance-deleted`), about 5 minutes. On a paid account, the equivalents are the instance quota, free IP addresses in the subnets, and the budget alarm that might fire mid-incident.

### Attempt 3: a match

Restored to 06:35:45, half a minute after the good state and a minute before the accident: the same 350 and 150, the same fingerprint.

### The real RTO

The restore took 10–11 minutes every time. For this drill, the whole recovery was:

| Step | Time |
|---|---|
| Waiting for the transaction log to be shipped | ~2 minutes |
| The restore | 10–11 minutes |
| Verifying the copy | ~1 minute |
| Deleting a wrong copy and retrying (twice) | ~35 minutes |

The restore is the predictable part. In a real incident you'd add noticing the problem, deciding which time to restore to, and then either switching the app over (losing every good write since that time) or copying the damaged rows back into production (part 1, 5.10). That last step isn't automated in this demo. In Ledgerline it would be one `UPDATE … FROM` per affected table, joining the restored copy's rows by id, written and reviewed under pressure. Practise it before you need it.

### What else came up

> **A "paused" database that wasn't.** Staging's database had been stopped at the end of Chapter 4. When the drill started, it was already `available`: **Amazon RDS automatically starts a stopped instance after seven days** (Chapter 2 warned about it, and it happened anyway), and it had been running, and billing, since. Stopping is a pause of up to a week, not an off switch. For longer breaks, destroy the environment (the final snapshot keeps the data) or stop it again weekly.

> **PowerShell prompts for missing parameters.** Running `.\scripts\drill.ps1` without a command opens a `Command:` prompt, which happily accepted `5` and even a pasted `Set-ExecutionPolicy` line before refusing them. The script's `ValidateSet` stopped anything worse; always pass the command: `.\scripts\drill.ps1 prepare`.

> **Expired sign-ins.** Every AWS command failed with *"Your session has expired. Please reauthenticate using 'aws login'"* after a break. `aws login --profile ledgerline`, then check with `aws sts get-caller-identity`.

---

## 5B.5 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker compose up --build` takes many minutes on `COPY . .` or `chown`; `transferring context` shows GB | Large folders (Terraform providers, build output, virtualenvs) in the build context | Add them to `.dockerignore`; `docker builder prune -f` |
| Docker Desktop: `500 Internal Server Error … dockerDesktopLinuxEngine/_ping`, or stuck "stopping the engine" | The engine hung (often after a huge build or a full disk) | Stop the Docker processes, `wsl --shutdown`, start Docker Desktop; check free disk space. Don't use "Clean / Purge data" unless you mean to delete volumes |
| `explain_queries` says `pg_stat_statements` isn't enabled | Postgres started without the library preloaded | Recreate the `db` service after adding the `command:` line: `docker compose up -d --force-recreate db` |
| Plans still show the old single-column indexes after `0003` | Migration not applied, or statistics stale | `alembic current`; `ANALYZE invoices;` |
| An index exists but `\d invoices` shows it as `INVALID` | A `CONCURRENTLY` build failed part-way | `DROP INDEX CONCURRENTLY …;` then run the migration again |
| `load_volume` refuses to run | Not development, or not Postgres | It's for local development databases only, by design |
| `start-db-instance`: `InvalidDBInstanceState … not in one of the following statuses: stopped` | The database is already running (possibly auto-started after 7 days) | Nothing to start; carry on |
| `python -m app.drill`: `No module named app.drill`, or the task fails at once | Staging runs an image from before the drill existed | Push, and wait for deploy-staging to finish |
| `drill.ps1`: `running scripts is disabled on this system` | PowerShell's execution policy | `Set-ExecutionPolicy -Scope Process Bypass` (this window only) |
| `drill.ps1` asks `Command:` | No command given | `.\scripts\drill.ps1 prepare` (or `state`, `accident`) |
| Restored copy: `No Drill Co tenant here` | Restore time before the data was committed (or a local time instead of UTC) | Restore to the safe restore point or later, in UTC (`…Z`), before the accident |
| `InstanceQuotaExceeded` on restore | Free plan instance limit; a copy still being deleted counts | `aws rds wait db-instance-deleted --db-instance-identifier …`, then retry |
| `restore-time` rejected as too late | The log for that time hasn't been shipped yet | Wait until `LatestRestorableTime` is later than the time you want |
| The drill task can't connect to the restored copy | Restored without the subnet group or security group | Pass `--db-subnet-group-name` and `--vpc-security-group-ids` (or modify the instance afterwards) |
| `Your session has expired. Please reauthenticate using 'aws login'` | Console sign-in credentials expired | `aws login --profile ledgerline` |

---

## 5B.6 Checklist

You've seen the data layer in practice when you can:

- [ ] Load realistic volume, with realistic skew, into a development database in minutes
- [ ] Find the most expensive statements with `pg_stat_statements`, and explain one with `EXPLAIN (ANALYZE, BUFFERS)`
- [ ] Design an index from a query's shape, add it with a migration that doesn't lock writes, and show the before and after
- [ ] Name a query that no index will fix, and the design options for it
- [ ] Point-in-time restore a managed database into a new instance with the right network settings
- [ ] Prove the restored data is right against something recorded beforehand
- [ ] State your measured restore time, and what else a real recovery would add to it
- [ ] Explain how you'd repair one customer's data without rolling back everyone else's
- [ ] Clean up completely: the restored copy, and a staging environment that won't quietly restart

---

**Next:** Chapter 6, Architecture evolution: queues and workers for work that outlives a request, the outbox pattern, scheduled jobs, and when (and when not) to split a system into services.
