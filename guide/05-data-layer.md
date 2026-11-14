# Chapter 5: The data layer

> **In this chapter:** the part of a system that outlives every other part. How far one Postgres database can take you, schemas that age well, finding the queries that are actually slow, indexes (and the problems they can't fix), changing big tables safely, connections and pooling, scaling reads with bigger machines, replicas and caches, the data that lives outside the database, backups and restores at scale, and deleting data properly.
>
> **Part 2, [The data layer in practice](05b-data-layer-demos.md),** loads a million invoices into Ledgerline and fixes what that breaks, then deliberately damages the staging database on AWS and restores it to the minute before.

---

## 5.1 Why it matters

Code can be rewritten in a weekend and redeployed in minutes. Data can't. Every row your users created is something you have to keep correct, keep available, keep private and eventually delete, through every version of the code that will ever touch it.

Three things make the data layer different from the rest of the system:

- **It's the hardest thing to change.** A table with 50 million rows and 20 queries reading it can't be renamed, split or re-typed in one step (Chapter 2, expand and contract). Early schema decisions stay with you for years.
- **It's where most performance problems live.** When a page is slow, the cause is usually a query: one that reads every row, or one that runs 200 times per request. More servers don't help; they make it worse.
- **Its failures are the only ones you can't fully recover from.** A crashed server is replaced automatically. A wrong `UPDATE` on production, a lost backup or a leaked table is permanent.

The good news is that a well-run relational database takes a product much further than most teams expect. Most of this chapter is about using one database well before adding a second.

> **Measure before you change anything, and change one thing at a time. Most data-layer "scaling" work is fixing four slow queries.**

---

## 5.2 One database, for longer than you think

### Postgres as the default

For a new product with ordinary needs (users, accounts, records, workflows, reporting), a managed **PostgreSQL** database is the default worth deviating from only for a reason. It's open source, every cloud runs it as a managed service, and it covers far more than tables:

| Need | Postgres feature | When you'd outgrow it |
|---|---|---|
| Relational data, transactions, constraints | The core | Rarely; you'd split by tenant or domain first |
| Semi-structured data | `jsonb` columns, GIN indexes | When the structure is the whole product (a document store's flexibility matters more than joins) |
| Full-text search | `tsvector`, `pg_trgm` | Relevance tuning, facets, typo tolerance across many languages, search as a core feature |
| Vector similarity (embeddings) | `pgvector` extension (available on RDS, Aurora and most managed services) | Hundreds of millions of vectors, or search latency that competes with everything else for the same machine |
| Job queues | `SELECT … FOR UPDATE SKIP LOCKED` | Thousands of jobs a second, or fan-out to many consumers (Chapter 6) |
| Locks across processes | Advisory locks (Ledgerline's migration lock, Chapter 2) | Rarely |
| Time series | Partitioned tables, BRIN indexes | Very high write rates, long retention: a time-series database |

Each row in that table is one fewer system to run, back up, secure, monitor and keep consistent. Adding a database is easy; operating two is a permanent cost.

Other relational databases (MySQL, SQL Server) are perfectly good choices, especially where a team already knows them. The advice in this chapter applies with different syntax. The reason to choose a **document store** (MongoDB, DynamoDB, Firestore) is a data model or scale pattern that really fits it: single-key access at huge scale, or data that's naturally one document per entity. "We don't want to write migrations" is not a good reason; the schema still exists, it's just enforced by every piece of code that reads the data instead of by the database.

### Managed, not self-run

Run the database yourself only if you have a reason and the people for it. A managed service (Amazon RDS or Aurora, Google Cloud SQL or AlloyDB, Azure Database for PostgreSQL, or Postgres-focused providers such as Neon, Supabase and Crunchy Bridge) handles patching, backups, point-in-time recovery, failover and monitoring. On a single VM (Chapter 4), all of that is yours.

### Versions are on a clock

Postgres releases a major version every autumn and supports each for five years. Cloud providers support versions a little longer, then charge for staying:

| Version | Community end of life | Amazon RDS end of standard support |
|---|---|---|
| PostgreSQL 14 | 12 November 2026 | 28 February 2027, then Extended Support fees |
| PostgreSQL 15 | November 2027 | 29 February 2028 |
| PostgreSQL 16 (Ledgerline) | November 2028 | 28 February 2029 |
| PostgreSQL 17 | November 2029 | 28 February 2030 |
| PostgreSQL 18 (on RDS since November 2025) | November 2030 | 28 February 2031 |

Minor versions (16.13 → 16.14) are security and bug fixes; let the provider apply them automatically in a maintenance window. Major upgrades change behaviour and need testing: plan one roughly every two to three years, rehearsed on a restored copy of production (5.10 shows how to make one).

> **Starting a new project today, use the newest version your provider supports.** PostgreSQL 18 adds `uuidv7()` (time-ordered IDs, 5.3), "skip scan" for multi-column indexes (5.5) and asynchronous I/O. Ledgerline uses 16 because that's what it started on; it's a good example of a version you'll upgrade later, not now.

---

## 5.3 A schema that ages well

Most schema regrets come from a handful of early decisions. Get these right and the rest can change later.

**Primary keys.** Three common choices:

| Key | Pros | Cons |
|---|---|---|
| `bigint` identity (1, 2, 3…) | Small, fast, ordered | Guessable; leaks volume ("invoice 1043"); IDs must come from the database |
| Random UUID (v4) | Generated anywhere; safe to expose | Large (16 bytes; 36 as text); random inserts scatter across indexes, so write-heavy tables slow down as they grow |
| Time-ordered UUID (v7) | Generated anywhere; inserts land at the end of the index like a sequence | Reveals creation time; built in from PostgreSQL 18, a library before that |

Ledgerline uses UUIDs stored as 36-character text, which is the most common and least efficient version of the second row. It's fine at today's scale. If it ever matters, new tables would use the native `uuid` type with v7 values.

**A tenant column on every tenant-owned table.** In multi-tenant B2B products, every row that belongs to a customer carries `tenant_id`, even where it could be found through a join. It makes every query filterable by tenant, makes indexes start with it (5.5), makes deleting or exporting one customer's data a simple query (5.11), and makes database-level isolation (row-level security, Chapter 7) possible later.

**Money as integers.** Store amounts in minor units (paise, cents) as `bigint`, with the currency next to them. Floating point can't represent 0.1 exactly. Ledgerline stores `total_minor` and `currency`.

**Time.** Always `timestamptz` (stored as UTC), never local time without a zone. Add `created_at` and `updated_at` to everything; you'll want them for debugging, sorting and syncing.

**Constraints in the database.** Not-null, unique, foreign keys and check constraints are enforced for *every* writer: the app, a script, a migration, a future service in another language. Application-level validation is for friendly error messages; database constraints are for correctness. Ledgerline's "same file uploaded twice" check is a unique constraint on `(tenant_id, sha256)`, which no code path can bypass.

**Status columns as text with a check, not a database enum.** Adding a value to a text column with a check constraint is an easy migration; removing a value from a Postgres `ENUM` type isn't possible without recreating it.

**JSON for what's genuinely variable.** A `jsonb` column is right for data whose shape varies or that you only read whole (Ledgerline's `flags`, its audit `before`/`after`). If you filter, join or report on a field regularly, it should be a real column.

**History: audit table or soft delete?** "Soft delete" (a `deleted_at` column) keeps deleted rows in the same table, and every query must remember to exclude them; the one that forgets is a data leak. An append-only **audit table** of changes (Ledgerline's `audit_events`) keeps history without polluting the live tables. Choose deliberately; don't add `deleted_at` to everything by reflex.

---

## 5.4 Finding the slow queries

Don't guess which queries are slow. The database knows.

### What's slow overall: `pg_stat_statements`

The `pg_stat_statements` extension records every distinct statement the database runs, with how many times, the total and average time, and how much data it read. Sort by **total time** to find what costs the most overall: a 5 ms query run 50,000 times an hour matters more than a 2-second report run twice a day.

```sql
SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time::numeric, 1) AS mean_ms,
       left(query, 100) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

It's loaded by default on Amazon RDS (run `CREATE EXTENSION pg_stat_statements;` once); locally, Postgres needs `shared_preload_libraries=pg_stat_statements` (Ledgerline's `docker-compose.yml` sets it). Reset it with `pg_stat_statements_reset()` before a measurement, so old activity doesn't drown the new.

> **It records statement text, including secrets.** In Ledgerline's demo, the top-statements list showed `ALTER ROLE ledgerline_app PASSWORD 'app-dev-pw'` in full. Parameterised queries appear with `$1` placeholders, but statements that embed a literal (role passwords, some migrations) are stored as written. Treat access to `pg_stat_statements` like access to the data, and set passwords in ways that don't log them (`\password` in psql, or a pre-hashed `SCRAM-SHA-256$…` value).

### Why one query is slow: `EXPLAIN (ANALYZE, BUFFERS)`

`EXPLAIN` shows the plan Postgres chose; `ANALYZE` runs the query and adds real timings and row counts; `BUFFERS` shows how many 8 KB pages it read. Things to look for:

| In the plan | Means | Usually |
|---|---|---|
| `Seq Scan on invoices` | Read every row of the table | Fine on small tables; a missing index on large ones |
| `Index Scan using …` | Found rows through an index, then read each row | Good, for a small fraction of the table |
| `Index Only Scan` | Answered from the index without touching the table | Best; needs the right columns in the index and a recently vacuumed table |
| `Bitmap Heap Scan` | Collected matching rows from an index, then read them in page order | Normal for medium fractions, or for combining two indexes |
| `Sort` with `external merge Disk:` | The sort didn't fit in memory | An index in the right order avoids the sort entirely |
| `rows=10` estimated vs `rows=700000` actual | The planner's statistics are wrong | `ANALYZE` the table; maybe extended statistics |
| `Parallel` … | Several workers split a big scan | It's still reading everything, just faster |

For the plan of a query your ORM builds, either log the SQL or compile it to text (Ledgerline's `scripts/explain_queries.py` runs the app's own query functions through `EXPLAIN`, so what you measure is exactly what production runs).

### Slow queries in production

- **Log the slow ones:** `log_min_duration_statement = 500` (milliseconds) writes every statement slower than that to the database log. `auto_explain` can log their plans too.
- **Your managed service's dashboard:** on AWS, CloudWatch Database Insights (which absorbed Performance Insights) shows load by query and by wait type. Similar views exist on every cloud.
- **Traces:** Chapter 3's database spans show which *request* ran which queries, and how many. That's how you find the next problem.

### The N+1 query

The most common ORM performance bug: load 50 invoices, then for each one, lazily load its vendor. That's 51 queries where 1 or 2 would do. Each is fast; together they're slow, and they get slower as the page grows. A trace shows them as a ladder of identical database spans. The fix is eager loading (`selectinload`, `joinedload` in SQLAlchemy; `include`, `prefetch_related` and so on in other ORMs), or a query that joins.

---

## 5.5 Indexes, and what they can't fix

### How a composite index is used

A B-tree index on `(tenant_id, status, created_at)` is sorted by tenant, then status within tenant, then time within that. It can answer:

- `WHERE tenant_id = ?` (the first column)
- `WHERE tenant_id = ? AND status = ?`
- `WHERE tenant_id = ? AND status = ? ORDER BY created_at DESC LIMIT 200`: it walks the index backwards and stops after 200 rows, without sorting anything

It generally can't help `WHERE status = ?` alone, because status isn't the first column. (PostgreSQL 18's skip scan can use it when the leading column has few distinct values, but don't design around that.)

The rule for column order: **columns compared with `=` first, then the column you sort or range-filter on.** Design each index for a specific query you've measured, not for a column that "might be searched".

Other kinds worth knowing:

- **Covering (`INCLUDE`):** `(tenant_id) INCLUDE (cost_usd)` stores `cost_usd` in the index so a `SUM` can be answered by an index-only scan.
- **Partial:** `WHERE status = 'needs_review'` indexes only the rows a work queue looks at; tiny and fast.
- **Expression:** `lower(email)` for case-insensitive lookups.
- **GIN** for `jsonb`, arrays and full-text search; **BRIN** for huge, naturally ordered tables (logs, events by time).

### Foreign keys aren't indexed automatically

Postgres indexes a primary key and every unique constraint, but **not** the referencing side of a foreign key. Without an index on `invoices.document_id`, deleting a document scans all of `invoices` to check nothing references it. Index foreign-key columns unless you're sure you never delete or update the parent.

Checking a foreign key also costs something on every insert. In Ledgerline's bulk load, `pg_stat_statements` showed **950,000 calls** of an internal `SELECT … FROM ONLY "public"."documents"`: Postgres checking each new invoice's document, one row at a time.

### What indexes cost

Every index is written on every insert and on many updates, takes disk and memory, and has to be vacuumed. After Ledgerline's indexing work, its `invoices` table was **239 MB and its nine indexes 377 MB**: more space for the indexes than the data. Find indexes nothing uses and remove them (in a later release, after confirming):

```sql
SELECT indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid))
FROM pg_stat_user_indexes ORDER BY idx_scan, pg_relation_size(indexrelid) DESC;
```

### Index-only scans need a vacuumed table

An index-only scan can skip the table only for pages the **visibility map** marks as all-visible, and that map is updated by `VACUUM`. Right after a bulk load or a new index, the planner often won't choose it; once autovacuum has run, it might. In Ledgerline's demo, the same query used the covering index on one machine and an ordinary index scan on another. Plans depend on statistics and table state, which is one more reason to measure in an environment that resembles production.

### Counting is the problem indexes don't solve

An index makes **finding a few rows** fast. It doesn't make **counting or summing many rows** fast: `SELECT status, count(*) … GROUP BY status` for a tenant with 700,000 invoices still reads 700,000 index entries. In Ledgerline's demo, the list page went from 200 ms to 1 ms with the right index, while the status counts above it stayed at 100–800 ms whatever the indexes.

The options, from cheapest:

| Option | How | Trade-off |
|---|---|---|
| Don't count | "200+ invoices"; paginate with "next" rather than "page 1 of 3,500" | Product change; often fine |
| Estimate | The planner's estimate (`EXPLAIN`, or `pg_class.reltuples` for a whole table) | Approximate; fine for "about 1.2M" |
| Cache | Keep the counts in a cache for a minute (5.8) | Slightly stale; one more system |
| Summary table | A `tenant_status_counts` table updated in the same transaction as each status change | Exact; every status change now updates a shared row, which can become a hot spot under heavy concurrent writes |
| Materialized view | `REFRESH MATERIALIZED VIEW CONCURRENTLY` on a schedule | Stale between refreshes; refresh cost grows with the data |

Ledgerline hasn't changed yet: a few hundred milliseconds for the largest tenant is acceptable today. When it isn't, the summary table is the likely choice, since approval counts must be exact.

---

## 5.6 Changing big tables

Chapter 2 covered the rules: expand and contract, `CREATE INDEX CONCURRENTLY`, `NOT VALID` constraints, batched backfills and a `lock_timeout`. At volume, a few more things matter.

**`CONCURRENTLY` can fail and leave debris.** A concurrent index build that fails (a duplicate value for a unique index, a cancelled deploy, a deadlock) leaves an **`INVALID`** index: it slows every write and is used by no query. Check after migrations, and drop and rebuild any you find:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

**Know how long it will take before production finds out.** Building Ledgerline's four indexes on a million invoices took about 4–8 seconds on a laptop. On a busy production database with more data and less spare I/O, it can be many minutes. Time it on a restored copy of production first.

**Set a `statement_timeout` for the app** (for example 30 seconds), so one runaway query can't hold locks and connections forever. Migrations can override it.

**Don't drop the old index in the same release.** Ledgerline's migration `0003` adds composite indexes that make three single-column indexes redundant, but doesn't drop them. If a query you didn't measure still relies on one, dropping it in the same deploy is a production slowdown with no quick rollback. Drop them in a later "contract" migration, once `pg_stat_user_indexes` shows they're unused.

**Partition when one table grows without bound.** Splitting a table by time (monthly partitions of events, audit logs, extraction runs) keeps each index small, makes dropping old data instant (`DROP TABLE` on a partition instead of a huge `DELETE`), and lets queries skip irrelevant months. It also adds complexity: partition creation, unique constraints that must include the partition key, and queries that must filter on it. Consider it for tables heading to hundreds of millions of rows, or with a retention rule (5.11).

**Archive what nobody reads.** Ten-year-old invoices don't need to sit next to this week's. Moving cold data to cheaper storage (a separate table, object storage as Parquet, a warehouse) keeps the hot tables small.

---

## 5.7 Connections and pooling

Every Postgres connection is a separate server process using several megabytes of memory. A small database accepts on the order of a hundred connections (`SHOW max_connections;` on yours). Connections are easy to run out of, and the error, "too many connections" or "remaining connection slots are reserved", takes the whole app down.

Add up what you'll open at peak:

```
app processes × pool size per process
  + background workers × their pool size
  + migrations, admin tools, the monitoring agent
  + the same again during a rolling deploy (old and new tasks overlap)
```

Ledgerline's API uses SQLAlchemy's default pool: up to 5 connections plus 10 overflow per process. Two production tasks at peak can hold 30; during a deploy, briefly 60. That's comfortable on a small instance today. With ten tasks and workers, it wouldn't be.

**Serverless functions are the extreme case.** Each concurrent function instance opens its own connections; a traffic spike creates a connection storm (Chapter 4).

The fixes, in order:

1. **Size pools deliberately.** More connections than CPU cores rarely makes a database faster; it makes queries queue inside the database instead of in the app.
2. **A connection pooler** between the app and the database: many client connections share a few server connections.
   - **PgBouncer:** the standard open-source pooler. In *transaction* mode, a server connection is lent out per transaction, which is what makes it efficient, but session state (`SET`, advisory locks held across transactions, `LISTEN`) doesn't survive between transactions. Recent versions support prepared statements in this mode.
   - **RDS Proxy:** AWS's managed pooler. It also makes failovers faster for the app. It's charged per vCPU of the database, with a minimum of two (about $22 a month at US prices), so it's worth it for Lambda-heavy or connection-heavy apps, not by default.
   - Some managed providers include a pooler (Supabase and Neon, for example).
3. **Fewer, larger processes** for the app, each with a small pool.

---

## 5.8 Scaling reads: a bigger machine, replicas, caches

When the database itself becomes the bottleneck (after the slow queries are fixed), the options, in the order to try them:

### A bigger machine

Vertical scaling is unglamorous and works. Moving from 2 to 8 vCPUs, or adding memory so the working set fits in cache, takes minutes on a managed service (with a brief failover on Multi-AZ) and needs no code change. Single Postgres instances with hundreds of gigabytes of memory serve very large products. Do this before anything that changes the architecture.

### Read replicas

A replica is a copy that receives every change from the primary, a little later, and serves read-only queries. Use them for:

- **Reports, exports and analytics** that would otherwise compete with the app for the primary
- **Read-heavy pages** where slightly stale data is acceptable

The catch is **replication lag**, usually milliseconds and occasionally seconds or more. A user who saves a change and is then shown a page read from a replica may not see their own change. Route "read your own write" paths to the primary, or make replica use explicit per query rather than automatic.

> **A Multi-AZ standby is not a read replica.** On Amazon RDS, a Multi-AZ *instance* deployment keeps a standby in another availability zone purely for failover; it serves no queries. (A Multi-AZ *DB cluster* has two readable standbys, at a higher price.) Multi-AZ protects against losing a machine or a zone, not against slow queries, and **not against bad writes**: a mistaken `UPDATE` is replicated to the standby within milliseconds. That's what backups are for (5.10).

### Caching

A cache stores the result of expensive work so you can skip it next time. Start with the cheapest kind:

1. **HTTP caching** of public, rarely changing responses (a CDN, `Cache-Control` headers).
2. **In-process memory** for small, rarely changing data (configuration, tax rates). Each app process has its own copy; that's fine if a few seconds of difference doesn't matter.
3. **A shared cache** (Valkey or Redis) when several processes must see the same cached value, or the data is too big for each process to hold.

The usual pattern is **cache-aside**: look in the cache; on a miss, query the database and store the result with a time-to-live (TTL). The hard part is never the cache; it's **invalidation**. When the underlying data changes, either delete the cached entry in the same code path that changed it, or accept staleness up to the TTL. Prefer short TTLs over clever invalidation until you have a reason.

Things that go wrong:
- **Caching per-tenant data under a key without the tenant**, which serves one customer's data to another. Put the tenant in every key, and test it.
- **A thundering herd:** a popular entry expires and a thousand requests recompute it at once. Use a lock or "refresh before expiry" for hot keys.
- **The cache becomes a database.** Something stored only in the cache is lost when it restarts or evicts. A cache must always be rebuildable from the source of truth.

> **Redis or Valkey?** In 2024, Redis moved from an open-source licence to source-available ones. The Linux Foundation then launched **Valkey**, a fork under the original BSD licence, backed by AWS, Google Cloud, Oracle and others. Redis 8 (2025) added the open-source AGPLv3 as an option. For most teams the practical difference is the managed service: AWS's **ElastiCache for Valkey** is priced 20% lower (nodes) to 33% lower (serverless) than its other engines, and serverless Valkey starts at about $6 a month. Client libraries work with both.

### Sharding

Splitting data across several databases (often by tenant) is the last resort. It makes every cross-shard query, transaction, migration and report harder. Very few products need it, and those that do usually know years ahead. Extensions such as Citus, and providers that shard Postgres for you, exist for when that day comes. A B2B product's first step is usually simpler: move the one huge tenant to its own database (a "dedicated" deployment, Chapter 13).

---

## 5.9 Data outside the database

Some data doesn't belong in the database, and some data lives in several places at once. The principle: **the database is the source of truth; everything else is either a file the database points to, or a copy that can be rebuilt from it.**

### Files: object storage

Uploaded documents, images, exports and generated files go in object storage (S3 and equivalents), not in database columns. The database holds the **metadata and the key**: who uploaded it, its hash, size, type and where it is. Ledgerline stores invoice PDFs in S3 under keys that start with the tenant, and the `documents` table points to them.

What to get right:
- **Private by default**, with access through the app or short-lived signed URLs (Chapter 2's private bucket module).
- **Versioning and lifecycle rules**: recover from overwrites and deletes; move old files to cheaper storage classes; expire what's past retention.
- **Consistency between the two:** a database row pointing to a missing file, or files no row points to. Write the file first, then the row; sweep for orphans occasionally.

### Search

Postgres full-text search (`tsvector` with a GIN index, plus `pg_trgm` for fuzzy matching) handles "find invoices mentioning *Acme*" well. A dedicated search engine (OpenSearch or Elasticsearch, or lighter ones such as Meilisearch and Typesense) becomes worth it when search *is* the product: relevance tuning, facets, typo tolerance, many languages, or search load you don't want on the primary database. It's then a **copy** of the data that must be kept in sync and can be rebuilt.

### Vectors (AI products)

Embeddings for retrieval (Chapter 9) can live in Postgres with `pgvector`, next to the rows they describe, filtered by tenant in the same query and covered by the same backups. That's the right start for most products. Dedicated vector databases earn their place at very large scale or with specialised search needs. Either way, keep the embedding model and version with each vector: changing models means re-embedding everything, and you'll want to know which rows are done.

### Analytics

Running heavy analytical queries on the production database competes with users. The path:

1. Queries against a **read replica**.
2. A nightly or continuous **export** to a warehouse or lakehouse (BigQuery, Snowflake, Redshift, Databricks, or files queried with DuckDB), often by **change data capture** (CDC) from the database's write-ahead log.
3. Product analytics events from the app go to their own pipeline, not to production tables.

### Keeping copies in sync

When one action must update the database *and* something else (a search index, a cache, a message to another service), doing both in the request is fragile: the second step fails and they disagree. The robust pattern is the **outbox**: in the same database transaction, write the change and a row describing it to an `outbox` table; a separate worker reads the outbox and updates the other systems, retrying until they succeed. It's covered with queues in Chapter 6.

---

## 5.10 Backups and restores at scale

Chapter 2 introduced RPO, RTO and the restore drill. This section is about what they look like on a real managed database, from doing it (part 2).

### How point-in-time recovery works

A managed database takes a daily **snapshot** and continuously archives the **write-ahead log** (every change). To restore to 14:32:10, it starts from the snapshot before that time and replays the log up to that second. On Amazon RDS:

- The log is shipped about **every five minutes**, so the most recent few minutes can't be restored yet. The instance's `LatestRestorableTime` says how far forward you can go. This is also your worst-case data loss (RPO) if the whole database is lost.
- You can restore to any second within the **retention period** (1 to 35 days; 1 on AWS's Free plan). Retention is how far *back* you can go, which matters for mistakes noticed late.
- **A restore always creates a new database.** The original is untouched. That's what makes restores safe to practise, and it means recovery is never just "click restore".

### Two kinds of disaster

| | Infrastructure failure | Logical error |
|---|---|---|
| Examples | A disk, a machine or a zone fails | A bad deploy, a wrong `UPDATE`, a deleted customer, ransomware |
| Multi-AZ helps? | Yes: automatic failover in a minute or two | **No:** the mistake is replicated to the standby instantly |
| Recovery | Usually automatic | Restore to just before the mistake, then repair |

Most real data loss is the second kind. And the second kind is never just a restore; it's a decision:

- **Switch over:** point the app at the restored database. Fast and simple, but every *good* write since the restore point is lost: the invoices uploaded after the mistake, and the approvals.
- **Copy back:** keep production running, and copy just the damaged rows from the restored database into production. Nothing good is lost, but it takes someone who understands the data, under pressure. Practise this one.

### What a restore doesn't bring back

On RDS, a restored instance gets the account's **default security group, parameter group and subnet group** unless you specify them, and no Multi-AZ, alarms or Terraform management. Ledgerline's drill passes the subnet group and security group explicitly; without them, nothing can connect to the copy. Write the full restore command down *before* you need it.

### What your RTO really is

The restore itself took **10 to 11 minutes** for Ledgerline's small staging database, consistently. Restore time grows with database size; large databases take hours, and their storage keeps loading in the background after the instance reports "available", so the first queries are slow. A real RTO is:

```
noticing the problem  +  deciding what to do and which time to restore to
  +  the restore itself  +  verifying the copy  +  switching over or copying back
```

The restore is often the smallest part. The drill is how you find out the rest.

### Lessons from the drill

Ledgerline's first two restores (part 2) both "succeeded" and both produced the wrong data:

1. **Restoring to the exact second of the last good change missed it.** The timestamp was rounded down to the second; the change committed a fraction of a second later. **Restore to a point safely after the last good change and before the mistake**, and verify the result against something you recorded beforehand.
2. **The account couldn't hold a second database** while the first copy was still being deleted (a Free plan quota). Restores need room: instance quotas, subnet IP addresses, budget. Find the limits in a drill, not during an outage.

### Protecting the backups themselves

From Chapter 2: separate permissions, deletion protection and, as data matters more, copies in another **account** and **region** (AWS Backup and its equivalents do this on a schedule) and immutable storage. Add one more: an occasional **logical export** (`pg_dump`) that you restore somewhere else. It proves the data can leave the provider, which is your escape hatch and an answer enterprise customers ask for.

---

## 5.11 Retention, deletion and privacy

Data you don't keep can't leak, doesn't need backing up and costs nothing.

- **Decide retention per kind of data:** invoices may have a legal minimum (often several years for financial records); debugging logs, days; LLM prompts and raw responses, as short as you can manage (Chapter 9). Write it down; enterprise customers will ask (Chapter 13).
- **Make deletion real, and testable.** "Delete this customer" must reach every table, the object storage, the search index, the cache and the analytics copies. A `tenant_id` on every row makes the database part a set of simple queries; the copies are the hard part, which is another reason to keep fewer of them.
- **Backups contain deleted data.** You can't edit a backup. The usual position (and what regulators generally accept) is that deleted data in backups expires with the backup's retention and is never restored into use; if a restore happens, deletions are re-applied. Document it.
- **Minimise what you collect,** especially personal data and sensitive documents. Ledgerline's demos use synthetic invoices for exactly this reason.
- **Encrypt in transit and at rest.** Managed databases encrypt storage when asked (Ledgerline's Terraform sets `storage_encrypted = true`); connections should require TLS (Ledgerline's database URL uses `sslmode=require`).
- **Least privilege:** the app connects as a user that can read and write rows but not change the schema (Chapter 2); people get read-only access to production data rarely, temporarily and logged (Chapter 8).

---

## 5.12 By stage

| Stage | Database | Performance | Reads and caching | Backups and recovery |
|---|---|---|---|---|
| MVP | One managed Postgres, smallest size; constraints in the schema | Fix what's visibly slow; avoid N+1 | None | Automated backups on; one restore by hand |
| Early production | Same; Multi-AZ for production; current major version | `pg_stat_statements`; slow query log; indexes designed per query; statement timeout | In-process caching of static data | PITR with 7+ days; scripted drill; restore commands written down |
| Growth | Bigger instance; a pooler; partitioning for unbounded tables | Load testing at realistic volume before big launches; index reviews; unused index cleanup | Read replica for reports; a shared cache (Valkey) for hot data; summaries instead of counts | Cross-account and cross-region copies; drills on a schedule with alerts; copy-back practised |
| Enterprise | Dedicated databases for the largest tenants; possibly sharding | Query performance budgets per endpoint; capacity planning | Analytics in a warehouse via CDC; search and vectors as rebuildable copies | Contractual RPO/RTO; audited drills; immutable backups; retention and deletion evidence |

---

## 5.13 By product type

- **B2B SaaS:** tenant on every row, indexes that start with it, and one or two very large tenants that dominate every performance problem (Ledgerline's demo gives one tenant 70% of the rows on purpose). Per-tenant export and deletion will be asked for. Dedicated databases for the biggest customers often come before any sharding.
- **AI and agentic products:** a lot of write-heavy, append-only data (runs, steps, tool calls, token counts, costs) that grows much faster than the business data. Partition it by time and decide its retention early. Keep embeddings with the rows they describe while you can. Store model, prompt version and cost per run (Ledgerline's `extraction_runs`); you'll need them for evaluation and billing (Chapters 9 and 12).
- **Internal tools:** often read from other systems' databases. Use a replica or an export, never the other system's primary, and never write to a database another team owns.
- **Consumer products:** more users, less data each, and spikier traffic. Connection limits, caching and read replicas arrive earlier; analytics events are a big, separate pipeline from day one.

---

## 5.14 Challenges and watch-outs

- **Testing with 30 rows.** Every query is fast on a development database. Generate realistic volume, with a realistic skew (one huge tenant), before you trust a design (part 2 does this).
- **Indexing by guesswork.** An index on every column that appears in a `WHERE` clause slows every write and still misses the multi-column indexes the real queries need.
- **The count on every page.** `count(*)` over a large tenant on every page view. Indexes won't save it.
- **ORM convenience hiding query volume.** Lazy loading in loops; whole rows loaded to read one column.
- **Connection exhaustion during deploys** or traffic spikes, from pools sized in isolation.
- **Replica lag surprises:** "I saved it and it disappeared."
- **A cache with no tenant in the key.** One customer sees another's data.
- **Multi-AZ mistaken for backup.** It protects against hardware, not against mistakes.
- **A restore nobody has done**, or one done once, years ago, on a database a tenth of today's size.
- **Copies nobody can rebuild:** a search index or cache treated as the source of truth.
- **Old major versions** quietly moving into paid extended support.
- **Signals you've outgrown your setup:** CPU or I/O on the database regularly above 70%; slow queries you can't index away; connection errors at peak; reports that slow the app; restore times longer than your RTO; one tenant's activity slowing the others.

---

## 5.15 Exercise

For each situation, what would you do first, and what would you not do yet?

1. The invoice list takes 3 seconds for your biggest customer and 50 ms for everyone else.
2. The database's CPU sits at 90% every weekday from 9 to 11.
3. After moving to serverless functions, the app intermittently fails with "too many connections".
4. A customer asks how much data they'd lose, and for how long they'd be down, if your database failed.
5. Someone ran a script that set every invoice of one tenant to "rejected" 40 minutes ago. Other tenants have been working normally since.
6. You want semantic search over invoice line items for an AI feature. Someone proposes a dedicated vector database.

<details>
<summary>Suggested answers</summary>

1. **Measure the query** for that tenant with `EXPLAIN (ANALYZE, BUFFERS)`. Almost certainly an index that doesn't match the query's shape (tenant, then sort column), or a count over all their rows. Don't scale the database or add a cache: the problem is one query, and it'll get worse as they grow.
2. **Look at `pg_stat_statements` sorted by total time** for that window: usually a few queries, or a report someone runs every morning. Fix those, or move the report to a replica. Then, if the load is real and spread out, a bigger instance. Not sharding, not a rewrite.
3. **A pooler** (RDS Proxy is designed for exactly this, or PgBouncer), and small pools per function. Raising `max_connections` only moves the limit and costs memory.
4. **Answer from what's configured, and what you've measured:** RPO about 5 minutes with point-in-time recovery (minutes of transaction log not yet archived); seconds if a Multi-AZ standby takes over. RTO: failover in a minute or two; a restore from backup, whatever your last drill measured, plus the time to notice and decide. If you haven't done a drill, do one before you answer.
5. **Don't roll the whole database back:** that would also undo 40 minutes of every other tenant's work. Restore a copy to just before the script ran, then **copy back** that one tenant's invoice statuses from the copy to production, and check the audit trail for anything legitimately changed since. Then find out why a script could do that (permissions, a review, a dry-run mode).
6. **Start with `pgvector`** in the existing database: one less system, tenant filtering in the same query, already backed up. Move to a dedicated store only if volume or latency proves it necessary, which you'll know from measurements.

</details>

---

## 5.16 Cost

Rough monthly costs, October 2026 list prices, US regions (others are typically 10–30% higher). Orders of magnitude, not quotes.

| Item | Approx. monthly | Notes |
|---|---|---|
| Smallest managed Postgres (2 vCPU burstable, 1 GB, e.g. RDS `db.t4g.micro`) | ~$12–20 | Covered by free tiers or credits for a while; Ledgerline's staging |
| Small production database (`db.t4g.medium` class, 4 GB) | ~$50 single-AZ, ~$100 Multi-AZ | Multi-AZ doubles the instance cost |
| Storage | ~$0.12 per GB | Plus I/O on some storage types and engines |
| Backups | Free up to the database's size, then per GB | Long retention and manual snapshots accumulate quietly |
| Read replica | Another instance | Same size as the primary, usually |
| RDS Proxy | From ~$22 | Per vCPU of the database, minimum 2 |
| ElastiCache Serverless for Valkey | From ~$6 | Grows with data stored and requests |
| Extended support for an old major version | Can exceed the instance price | Upgrading is cheaper |
| A restore drill (Ledgerline's) | Cents | One small instance for an hour |

What the table hides: the database is usually the **largest fixed cost** of a small B2B product, because it can't scale to zero and production needs a standby. And, as everywhere in this guide, people's time: a slow-query investigation is hours; a missing backup is weeks, or the company.

---

## 5.17 Demo

[Part 2, The data layer in practice](05b-data-layer-demos.md):

1. **Slow query → index:** load a million synthetic invoices into local Postgres, measure the app's own queries with `EXPLAIN` and `pg_stat_statements`, add indexes shaped like the queries with a safe migration, and measure again, including the queries indexes couldn't fix.
2. **Restore drill on RDS:** in staging, record a known state, make a "buggy script" reject 500 invoices, restore the database to the minute before, prove the copy matches, and time it. Including the two attempts that restored the wrong data.

---

## 5.18 Checklist

Your data layer is in good shape when:

- [ ] Your database is a managed service on a supported major version, with an upgrade plan before the end of standard support
- [ ] Constraints (not-null, unique, foreign keys, checks) enforce correctness in the database, not only in code
- [ ] Every tenant-owned table has a tenant column, and indexes for tenant queries start with it
- [ ] `pg_stat_statements` (or your provider's equivalent) is on, and someone looks at the top queries regularly
- [ ] Slow statements are logged, and traces show which requests ran which queries
- [ ] Indexes are designed for measured queries; foreign keys are indexed; unused indexes are found and removed
- [ ] You've tested your main pages with realistic data volume and skew
- [ ] Large counts and totals don't run on every page view, or you've measured that they're acceptable
- [ ] Migrations on big tables avoid long locks, and you check for `INVALID` indexes after them
- [ ] Peak connections (including deploys) fit within the database's limit, with a pooler where needed
- [ ] Caches have the tenant in every key, a TTL, and can be rebuilt from the database
- [ ] Files, search indexes and other copies are either pointed to by the database or rebuildable from it
- [ ] Point-in-time recovery is on, you know its retention and RPO, and you've measured an RTO in a drill
- [ ] The restore command is written down and tested, including network settings and quotas
- [ ] You know how you'd repair a logical error without losing everyone else's work
- [ ] Retention and deletion are defined per kind of data, including what happens in backups

---

**Next:** Chapter 6, Architecture evolution: queues and workers for work that outlives a request, the outbox pattern, scheduled jobs, and when (and when not) to split a system into services.
