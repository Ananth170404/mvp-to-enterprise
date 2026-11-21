# Chapter 6: Architecture evolution

> **In this chapter:** how a system's shape should change as it grows, and how not to change it too early. The modular monolith; moving slow and unreliable work out of requests into queues and workers; designing jobs that survive crashes, duplicates and poison messages; running and scaling workers; the dual-write problem and the outbox; scheduled jobs; webhooks and events; when splitting into services is worth it; and protecting the things you depend on.
>
> **Part 2, [Architecture evolution in practice](06b-architecture-demos.md),** moves Ledgerline's invoice extraction to an SQS queue and a separate worker: killed mid-job, retried, dead-lettered, and scaled from zero to three workers on AWS and back.

---

## 6.1 Why it matters

Most products start as one process that answers requests. That's the right start (Chapter 1). It stops being enough in recognisable ways:

- **Requests do slow work.** Calling a model, generating a PDF, sending emails. The user waits, the web server's memory is tied up, and a timeout anywhere loses the work.
- **Work is lost on restart.** Every deploy kills whatever the process was doing. In-process background tasks die with it (Chapter 2's "stuck in processing" problem).
- **Load arrives in bursts.** Month-end, a big customer's bulk upload, a webhook storm. A web tier sized for a normal day falls over, and one sized for the peak is mostly idle.
- **Different work needs different resources.** Page views need low latency and little memory; document processing needs a lot of memory and tolerates delay.
- **More people work on the code.** Changes collide, deploys get riskier, and someone proposes microservices.

Architecture evolution is answering these one at a time, with the smallest change that fixes the actual problem. The big rewrite ("v2 in microservices") is almost always the wrong answer.

> **The first architectural change most products need isn't services. It's a queue and a worker: one codebase, two process types.**

---

## 6.2 A modular monolith first

A **monolith** is one deployable application. A **modular** monolith has clear internal boundaries: modules that own their data and talk to each other through defined functions, not by reaching into each other's tables.

It gets you most of what people want from microservices (separation, ownership, testability) without the costs: one deploy, one database transaction across modules, function calls instead of network calls, one place to debug.

What keeps it modular:

- **Each module owns its tables.** Other modules call its functions; they don't query its tables directly.
- **Dependencies point one way.** Workflow can call extraction; extraction never imports the web layer.
- **The boundaries are visible in the code.** Packages per domain, and import rules enforced by a linter if the team is big enough to need it.
- **Shared code is small and boring:** configuration, database sessions, logging.

> **Ledgerline** is a modular monolith in miniature: `extraction.py` does the extraction work, `workflow.py` owns the invoice state machine, `checks.py` the validation rules, `jobs.py` the queue. Extraction takes an invoice ID and opens its own session; that's what made moving it to a separate process a change to its caller, not to the function itself (Chapter 1 designed it that way).

**One codebase, several process types.** The same code can run as different processes: the web server, a worker, a scheduler, a one-off migration. Each is deployed from the same image with a different command (Ledgerline's ECS task definitions: `uvicorn` for the app, `python -m app.worker` for workers, `alembic upgrade` for migrations). This is the twelve-factor "process types" idea, and it's the step that usually comes long before services.

---

## 6.3 Work that outlives a request

### What belongs in the background

Move work out of the request when it is:

- **Slow:** more than a second or two (model calls, file processing, reports)
- **Unreliable:** calls to things that fail or rate-limit (model providers, payment gateways, email, partner APIs)
- **Bursty:** arrives faster than you want to process it
- **Not needed for the response:** the user can see "processing" and come back

The request then does the minimum (validate, save, ask for the work) and returns. Ledgerline's upload went from waiting for extraction to returning in under 100 ms, while extraction took 10–15 seconds elsewhere.

### The options, from simplest

| Option | How it works | Good for | Breaks when |
|---|---|---|---|
| **In-process background task** (FastAPI `BackgroundTasks`, a thread) | Runs after the response, in the web process | Quick follow-ups that are fine to lose | A deploy or crash loses it; competes with requests for CPU and memory; no retries |
| **Database-backed queue** (a jobs table with `SELECT … FOR UPDATE SKIP LOCKED`; libraries such as procrastinate for Python, River for Go, Oban for Elixir, Solid Queue for Rails, pgmq) | Jobs are rows; workers claim them | Small to medium volume; enqueue inside the same transaction as the data change (6.6); no new infrastructure | Very high throughput; the queue load competes with the app on the same database |
| **A job library on a broker** (Celery, RQ, Dramatiq, Sidekiq, BullMQ; usually with Redis/Valkey or RabbitMQ) | The library handles retries, scheduling, routing | Rich features: chains, priorities, rate limits, periodic tasks | You now run and monitor a broker; its persistence settings decide whether jobs survive a restart |
| **A managed queue** (Amazon SQS, Google Cloud Tasks or Pub/Sub, Azure Service Bus or Queue Storage) | The cloud stores messages; your workers receive and delete them | Durable, scales without effort, nearly free at small volume, dead-letter queues built in | Long multi-step workflows; strict ordering needs FIFO variants with lower limits |
| **A log or stream** (Kafka, Kinesis, Redpanda) | An ordered, replayable log many consumers read | Event streams, many independent consumers, replay | Simple job queues: it's more machinery than a job needs |
| **A durable workflow engine** (Temporal, AWS Step Functions, Inngest, Restate) | Each step's result is recorded; a workflow resumes where it left off after any failure | Long, multi-step processes with waits, human approvals and compensation (Chapter 10's agents) | Simple one-step jobs: overkill |

There's no single right answer. A sensible default for most teams is the managed queue of the cloud you're on, or a database-backed queue if you want transactional enqueue and fewer moving parts. Add a job library when you need its features, not because it's what Python projects "use".

> **Ledgerline chose SQS** with a small hand-written worker (about 200 lines, `app/worker.py`): no broker to run, retries and a dead-letter queue built in, scaling on queue depth with standard ECS autoscaling, and a free local equivalent (ElasticMQ). Celery could use SQS as its broker later if Ledgerline needs chains or schedules; nothing in the job design would change.

---

## 6.4 Designing jobs that survive failure

Moving work to a queue doesn't make it reliable by itself. The job's design does.

### Assume every job runs at least once, and sometimes twice

Queues offer **at-least-once** delivery. A worker can finish the work and crash before acknowledging it; a network blip can deliver a message twice; SQS standard queues occasionally deliver duplicates. "Exactly once" exists only as at-least-once delivery plus idempotent handling.

So every job must be **idempotent**: running it twice has the same effect as running it once. Techniques:

- **Check state first.** "If this invoice is already approved, do nothing." (Never overwrite a person's decision with a retry.)
- **A version in the message.** Ledgerline's job carries the invoice's version when the job was queued (`status_changed_at`). If the invoice has changed since, the job is stale or a duplicate, and is skipped. In the demo, a deliberately duplicated message did its work once: one model call, not two.
- **Idempotency keys** for calls to external systems that support them (payment APIs, many partner APIs), so a retried call doesn't charge twice.
- **Upserts and unique constraints,** so a second insert fails or merges instead of duplicating.

### Messages carry references, not data

Put the ID and the version in the message, not the document. The worker reads current data from the database when it runs. Messages stay small (SQS's limit is 1 MiB, raised from 256 KiB in 2025, but small is still better), don't duplicate sensitive data into another system, and can't act on stale copies.

### Visibility timeouts and heartbeats

When a worker receives an SQS message, the message becomes invisible to other workers for the **visibility timeout**. If the worker deletes it in time, it's done. If not, because the worker crashed or the job ran too long, it reappears and another worker gets it.

That gives you crash recovery for free, but you must size it:

- **Too short:** a slow but healthy job reappears while it's still running, and runs twice.
- **Too long:** a crashed worker's job sits invisible for that long before anyone retries it.

The usual answer is a moderate timeout plus a **heartbeat**: while the job runs, the worker keeps extending its visibility. A dead worker stops the heartbeat, so its job comes back quickly. Ledgerline uses 120 seconds in AWS (30 locally for the demo) and extends every 60 seconds. In the local demo, a job whose worker was killed came back after 30 seconds, and the retry took over.

**Taking over a half-finished job** needs care in the job itself. Ledgerline's invoice was left in "extracting" by the dead worker. The retry (the message's second delivery, after the visibility timeout, so the first worker can't still be running) is allowed to take over that claim; a first delivery never is. Without that rule, the retry would see "extracting", assume someone else was working on it, and skip, leaving the invoice stuck.

### Retries, backoff and poison messages

- **Retry transient failures** with exponential backoff and jitter: a rate limit or a timeout often succeeds a few seconds later. Ledgerline retries model calls inside the job (Chapter 1), and the queue retries the whole job.
- **Separate business failures from infrastructure failures.** "The model couldn't read this invoice" is an outcome: record it, mark the invoice for review, delete the message. "The database is down" is a reason to retry the job later.
- **Cap the retries.** A message that fails every time (a bug, a corrupt payload) is a **poison message**. Retried forever, it wastes workers and money and can block a queue. After a fixed number of deliveries (3 in Ledgerline), the queue moves it to a **dead-letter queue (DLQ)**.
- **A dead-letter queue needs an owner.** Alarm on it (anything in it means something needs a person), keep messages long enough to investigate (14 days), and have a runbook: find the cause in the logs, fix it, then **redrive** the messages back to the main queue.

### Ordering

Most jobs don't need ordering, and designing for it costs throughput. If two jobs for the same record must not run out of order, make the job check state (the version check does this), or use FIFO queues with a message group per record. Don't assume a standard queue delivers in order.

### Timeouts and limits

Every job needs a maximum run time, enforced: a stuck call shouldn't hold a worker forever. Ledgerline's model calls time out at 120 seconds, and ECS replaces a worker whose health check (a heartbeat file updated by its loop) goes stale.

---

## 6.5 Workers: running, stopping, scaling

### Stopping gracefully

Workers are stopped all the time: every deploy, every scale-in. Containers get a signal (`SIGTERM`) and some seconds before they're killed (up to 120 on AWS Fargate). A worker should **stop taking new jobs, finish the current one, then exit**. Anything still running when the time runs out must be safe to retry, which it is, if 6.4 is done right.

### Scale on the queue, not on CPU

A worker waiting 10 seconds for a model uses almost no CPU while users wait. CPU-based autoscaling would never add workers. Scale on what users feel:

- **Queue depth** (messages waiting), or better, **backlog per worker**
- **Age of the oldest message**, which says how long the oldest job has waited

**Scaling to zero** saves money when there's no work, and costs latency when work arrives. In Ledgerline's AWS demo, 30 jobs arrived at an idle queue; the first worker started **about 3½ minutes later**: about 3 minutes for queue metrics to reach CloudWatch and trip the scaling alarm, then about 30 seconds for the task to start. Fine for staging; not for production, where Ledgerline keeps at least one worker running.

**Scaling decisions run on old data.** Queue metrics arrive in CloudWatch a minute or more late, and alarms evaluate them later still. In the demo, a third worker was added when only one job was left, and all three then sat idle for 12 minutes before scaling in. Expect scaling to overshoot and lag. Use cooldowns, keep scale-in conservative (never while jobs are in progress), and don't try to tune it to the second.

Approximate counts are approximate. SQS's "messages waiting" briefly went *up* in the demo (19, then 22) while nothing new was being added. Use these numbers for trends and alarms, not for logic.

### Concurrency is a limit you choose

The maximum number of workers, times the jobs each runs at once, is how many concurrent model calls, database connections and partner API calls you can make. Ledgerline's `worker_max_count` (3 in staging, 4 in production) is as much a **protection** for the database and the model provider's rate limits as it is a capacity setting (6.10, and Chapter 5's connection budget).

### Observability for queues

Chapter 3's signals, applied to background work:

- **Queue depth and the age of the oldest message**, with an alarm on age (Ledgerline: oldest waiting job over 15 minutes)
- **Dead-letter queue depth**, with an alarm on anything above zero
- **Job outcomes and durations** (Ledgerline's `ledgerline.jobs.processed` by outcome, and `ledgerline.jobs.wait` for time in the queue)
- **Traces across the queue.** The job message carries the W3C trace context, so the worker's trace links back to the upload request that asked for it.
- **Logs with the job ID and attempt** on every line, so "what happened to this job" is one query.

---

## 6.6 The dual-write problem and the outbox

The upload request does two things: it saves the invoice in the database, and it sends a job to the queue. These are two different systems, and there's no transaction that covers both:

| Order | If the process dies between the two |
|---|---|
| Send the job, then commit | The worker may get a job for an invoice that doesn't exist yet, or never will (the commit failed) |
| Commit, then send the job | The invoice exists, but no job was ever sent: it sits as "uploaded" forever |

Ledgerline commits first, then sends, and covers the gap with a **sweep**: invoices left "uploaded" for an hour (much longer than any backlog) or "extracting" for longer than any extraction can take are marked failed, for a person to retry. That's acceptable when a rare stuck item can be fixed by a click.

When it isn't acceptable (payments, messages to other companies, anything where a lost or duplicated action matters), use the **transactional outbox**:

1. In the **same database transaction** as the change, insert a row into an `outbox` table describing what must be sent ("extract invoice X, version V").
2. A separate **relay** process reads unsent outbox rows, sends them to the queue (or calls the other system), and marks them sent.
3. The relay may send a row twice if it crashes between sending and marking. Consumers are idempotent anyway (6.4).

Now the database transaction decides: either both the change and the intent to send exist, or neither does. Alternatives with the same effect:

- **A database-backed queue:** the job row *is* the outbox, inserted in the same transaction.
- **Change data capture** (Debezium and managed equivalents): stream committed changes from the database's log to a queue or stream.

The same problem appears whenever one action updates two systems: database plus search index, database plus cache, database plus a webhook to a customer. Recognise it, and decide deliberately between "sweep and repair" and "outbox".

---

## 6.7 Scheduled work

Every product grows jobs that run on a clock: nightly reports, cleanup, retention deletes (Chapter 5), reminders, the stuck-job sweep, billing runs.

**Where to run them:**

- A cloud scheduler that starts a task or sends a message: **EventBridge Scheduler** (AWS), **Cloud Scheduler** (Google Cloud), **Azure Functions timer triggers** or Logic Apps
- **Kubernetes CronJobs**
- A job library's scheduler (Celery beat, Sidekiq cron), which you then have to run exactly once
- Inside an existing long-running process (Ledgerline's worker runs the stuck-job sweep every 5 minutes, safe because the sweep locks rows with `SKIP LOCKED` and is fine to run on several workers at once)

**What goes wrong with schedules:**

- **Two runs at once:** the last run is still going, or two schedulers fire. Use a lock (an advisory lock, a lease row) or make the job safe to overlap.
- **Missed runs:** the scheduler was down, or the job failed silently. Monitor that it *ran*, not just that it didn't fail: a "dead man's switch" alerts when an expected heartbeat doesn't arrive.
- **Time zones and daylight saving:** a job "at 2am" that runs twice or not at all once a year. Schedule in UTC unless the business needs local time, and then test the transitions.
- **Big batches:** a nightly job that processes everything at once grows with the data until it no longer fits in the night. Prefer small, frequent, resumable batches.

Prefer **putting work on the queue** from the schedule over doing it in the scheduler. The schedule says "it's time"; the queue and workers bring retries, limits and visibility you already have.

---

## 6.8 Events, webhooks and integration

### Receiving webhooks

Partners and providers will call you (payment events, e-mail ingestion, e-signature, ERP updates). The pattern:

1. **Verify** the signature (shared secret or public key), and reject old timestamps to prevent replays.
2. **Store** the raw event, keyed by the sender's event ID, so a redelivery is recognised (most senders retry, and duplicates are normal).
3. **Respond `2xx` quickly**, within the sender's timeout, usually a few seconds.
4. **Process later**, from a queue, with the usual retries and dead letters.

Doing the work inside the webhook request means slow responses, sender retries, and duplicate processing.

### Sending webhooks and events

When customers' systems depend on your events (Chapter 11's integrations): sign them, retry with backoff for a day or more, record every attempt, let customers see and replay deliveries, and never block your own processing on a customer's slow endpoint. The outbox (6.6) is how you make "it happened" and "we'll tell them" agree.

### Publish/subscribe

When several parts of a system react to the same thing ("an invoice was approved": update the ledger, notify the clerk, sync to the ERP), a **topic** with several subscribers (SNS with SQS queues, Pub/Sub, EventBridge, Kafka) lets each part react independently with its own queue, retries and dead letters.

It's powerful and easy to overdo. Events make the flow of control invisible: nothing in the code that publishes "approved" tells you what happens next. Use events for genuinely independent reactions; use direct calls when one step needs another's result.

---

## 6.9 When to split into services

### Good reasons

- **Different scaling or resource needs** that one deployable can't serve. (Often solved by process types, 6.2.)
- **Independent deploys for independent teams.** When several teams wait on each other's releases, separate services let each ship on its own schedule.
- **A hard isolation boundary:** a component with different security or compliance requirements (card data, regulated data), or untrusted code.
- **A different technology** that's really needed for one part (a heavy ML runtime, a language with the right library).
- **A different reliability target:** something that must keep working when the main app is down (status page, auth).

### Bad reasons

- "Microservices are the enterprise architecture." Many large, successful products run on modular monoliths.
- The code is a mess. Splitting a mess produces a distributed mess: the same tangles, now with network calls, partial failures and version skew.
- To use a framework, a cloud service or a job title.
- Anticipated scale that hasn't arrived.

### What a split costs

Every boundary becomes a network call that can fail, be slow or be retried; data that was one transaction becomes eventual consistency (sagas, outboxes, compensations); debugging needs distributed tracing; local development needs several services; deploys need compatibility between versions; and someone has to own each service's on-call. Budget for all of it before splitting.

### How to split, when it's time

- **Find the seam first, in the monolith.** Make the module self-contained (its own tables, a clear interface) while it's still in one process. Ledgerline's extraction was a function with an ID-only interface long before it moved to a worker.
- **Strangle, don't rewrite.** Route one capability at a time to the new service, behind the same interface, and keep the old path until the new one is proven (the "strangler fig" pattern).
- **Data moves with ownership.** A service that shares another's database tables isn't separate; it's a monolith with extra steps.
- **Align with teams.** Systems tend to mirror the organisation that builds them (Conway's law). A service per team works; ten services for a team of five usually doesn't.

Note what part 2 did *not* do: the worker isn't a service. It's the same code, image and database, with a different process type. That's the step that fixes most of the problems in 6.1.

---

## 6.10 Backpressure and protecting dependencies

A queue moves load in time; it doesn't remove it. Something downstream still has limits: the model provider's rate limits, the database's connections, a partner API's quota.

- **Cap concurrency at the source:** the number of workers and the jobs per worker. Ledgerline's maximum of 3 workers is also its maximum of 3 concurrent model calls.
- **Back off on rate limits:** a `429` means slow down, not "retry immediately"; honour `Retry-After`.
- **Fair use across tenants.** One customer's bulk upload of 10,000 invoices shouldn't delay everyone else's single invoice by an hour. Options: per-tenant queues or message groups, per-tenant concurrency limits, or a priority lane for small jobs. Design for it before your largest customer finds it.
- **Shed load at the front door** when the queue's age passes what users will tolerate: tell users "processing is delayed" rather than accepting work you can't do for hours.
- **Circuit breakers** for dependencies that are down: stop calling for a while instead of piling up failures and timeouts.

---

## 6.11 By stage

| Stage | Structure | Background work | Scheduling and events |
|---|---|---|---|
| MVP | One process, modular code | In-process tasks for small things; slow work designed as functions that take IDs (Chapter 1) | A sweep for stuck work; cron on a server if anything |
| Early production | Modular monolith; web and worker process types from one image | A real queue (managed or database-backed); idempotent jobs; retries; dead-letter queue with an alarm | Cloud scheduler putting work on the queue; webhooks stored then processed |
| Growth | Clear module ownership; maybe one or two services where a boundary is real | Autoscaling on queue age; per-tenant fairness; outbox where dual writes matter; durable workflows for long processes | Pub/sub for independent reactions; outbound webhooks with retries and a delivery log |
| Enterprise | Services aligned with teams, each owning its data | Capacity planning per queue; isolation of the largest tenants; replay and audit of events | Event contracts versioned and documented; customer-visible delivery history; dead-man's switches on schedules |

---

## 6.12 By product type

- **B2B SaaS:** bursty, customer-driven batches (month-end, onboarding imports). Per-tenant fairness and visible progress ("312 of 1,000 processed") matter more than raw throughput. Integrations (webhooks in and out) arrive early.
- **AI and agentic products:** almost every useful operation is slow, rate-limited and occasionally wrong, so queues come early and concurrency limits double as cost limits. Multi-step agent runs (plan, call tools, wait for a human, continue) are where durable workflow engines earn their place (Chapter 10). Record every step; a crashed agent must resume, not restart and repeat its side effects.
- **Internal tools:** often a scheduled sync with other systems. The risks are silent failure (monitor that it ran) and load on someone else's system (rate-limit yourself).
- **Consumer products:** fan-out at scale (notifications, feeds, emails to millions) is a queue and pub/sub problem from early on; each message must be cheap and idempotent.

---

## 6.13 Challenges and watch-outs

- **Fire-and-forget background tasks** in the web process, lost on every deploy.
- **Jobs that aren't idempotent,** discovered when a customer is emailed or charged twice.
- **No dead-letter queue,** so a poison message retries forever; or a dead-letter queue nobody watches.
- **Visibility timeouts shorter than the job,** so slow jobs run twice.
- **Retries that can't take over a half-done job,** leaving records stuck in "processing".
- **The dual write ignored:** "saved" and "queued" disagree after a crash, and nobody notices.
- **Scaling workers on CPU** while the queue grows.
- **Scale-to-zero latency** surprising users, or idle workers billing after a burst.
- **Unlimited worker concurrency** exhausting database connections or the model provider's rate limit.
- **One tenant's batch starving everyone else.**
- **Cron jobs that silently stop running,** or run twice.
- **Splitting services before the modules are clean,** producing a distributed monolith.
- **Deploying something other than what you tested.** Ledgerline's first AWS deploy of this chapter shipped the previous commit: the new files were never committed (part 2).
- **Signals that the architecture needs to change:** requests timing out on slow work; work lost on deploys; queue age growing every day; one team constantly blocked by another's releases; a component with very different security or scaling needs.

---

## 6.14 Exercise

For each situation, what would you do first, and what would you avoid?

1. Users wait 20 seconds after clicking "Generate report", and some reports are lost when you deploy.
2. A worker occasionally sends the same "invoice approved" email twice.
3. A partner's webhook endpoint calls you; you process the event inside the request, and the partner says you time out and they retry.
4. Your largest customer uploads 5,000 invoices at month-end; other customers' single uploads wait two hours.
5. A team lead proposes splitting the app into eight microservices because "deploys are scary".
6. Your queue's "messages waiting" has grown slowly every day for a month.

<details>
<summary>Suggested answers</summary>

1. **Move report generation to a queue and a worker:** save a "report requested" row, queue a job with its ID, show progress, and let the worker write the result. Avoid: making the request timeout longer, or a thread in the web process (still lost on deploy).
2. **Make the email job idempotent:** record "approval email sent for invoice X" (a unique row, or a flag set in the same transaction as the send intent), and skip if present. If the email provider supports idempotency keys, use the invoice ID. Avoid: trying to make the queue "exactly once".
3. **Verify, store the raw event keyed by its ID, return 200, process from a queue.** Duplicates from their retries are then recognised and ignored. Avoid: just processing faster; it will be slow again one day.
4. **Fairness:** per-tenant concurrency limits or a separate lane for large batches, so small jobs keep moving. Show the big customer progress. Avoid: only adding workers; the batch will grow too, and you'll hit the model provider's rate limit.
5. **Ask what makes deploys scary.** Usually it's missing tests, no staging, no fast rollback, or big releases (Chapters 2 and 4), and microservices make every one of those harder. Fix those first; split later only along a real seam. Avoid: a rewrite.
6. **Arrival rate exceeds processing rate.** Look at job durations (did something get slower?), failure and retry rates (are retries eating capacity?), and the worker maximum. Then raise capacity, or make the jobs cheaper. Avoid: ignoring it until the backlog is days long; alarm on the oldest message's age.

</details>

---

## 6.15 Cost

Rough monthly costs, November 2026 list prices, US regions (others are typically 10–30% higher).

| Item | Approx. monthly | Notes |
|---|---|---|
| Amazon SQS | Free for the first million requests a month, then about $0.40 per million | Each receive, delete, heartbeat and empty long poll is a request; a single idle worker long-polling uses about 130,000 a month |
| A worker on Fargate (0.25 vCPU, 1 GB), always on | ~$11 | Per worker; Ledgerline's production minimum is 1 |
| The same, scaled to zero | Only the minutes it runs | Plus the start-up delay when work arrives (3½ minutes in the demo) |
| Idle time after a burst | ~12 minutes × workers per burst | The scale-in delay in Ledgerline's demo; cheap, but it adds up with large maximums |
| EventBridge Scheduler | Free for millions of invocations a month | For scheduled jobs that start tasks or send messages |
| Step Functions (standard workflows) | About $25 per million state transitions | For durable multi-step workflows; express workflows are priced differently |
| A self-run broker (Redis/Valkey, RabbitMQ) | A small instance, plus your time | Or the managed version (5.8 lists Valkey's) |

The bigger costs are elsewhere: the work the workers do (model calls, Chapter 12), and the engineering time that bad job design costs when duplicates and stuck records reach customers.

---

## 6.16 Demo

[Part 2, Architecture evolution in practice](06b-architecture-demos.md):

1. **Queue and worker, locally:** move extraction to ElasticMQ and a worker in Docker Compose; kill the worker mid-job and watch the retry take over; run three workers through a batch; send a poison message to the dead-letter queue.
2. **The worker on AWS:** SQS queues, a worker service that scales from zero on queue depth, alarms on dead letters and waiting time. Thirty invoices, from upload to the last extraction, minute by minute, and everything that went wrong getting there.

---

## 6.17 Checklist

Your architecture is evolving well when:

- [ ] The code has clear modules that own their data, even if it's one deployable
- [ ] Slow, unreliable or bursty work runs in workers, not in requests
- [ ] Web and workers are process types of the same codebase and image
- [ ] Every job is idempotent, and you've tested a duplicate delivery
- [ ] Messages carry IDs and versions, not data
- [ ] Visibility timeouts (or leases) fit your jobs, with heartbeats for long ones
- [ ] A retry can take over work left half-done by a dead worker
- [ ] Failures retry with backoff; poison messages go to a dead-letter queue that alarms and has a runbook
- [ ] Workers stop gracefully, and scale on queue depth or age, within a maximum you chose
- [ ] You alarm on the age of the oldest waiting job
- [ ] Traces and logs follow a job from the request that queued it to the worker that ran it
- [ ] Every dual write is either covered by a sweep you've accepted, or by an outbox
- [ ] Scheduled jobs are monitored for having run, and are safe to overlap or locked
- [ ] Webhooks are verified, stored, acknowledged quickly and processed from a queue
- [ ] Concurrency limits protect the database and the providers you depend on, and no tenant can starve the others
- [ ] Any service split has a real reason, a clean seam and an owner

---

**Next:** Chapter 7, Auth, identity and multi-tenancy: real sign-in instead of the development login, single sign-on for enterprise customers, roles and permissions, and making sure one tenant can never see another's data.
