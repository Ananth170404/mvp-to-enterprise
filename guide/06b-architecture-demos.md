# Chapter 6, part 2: Architecture evolution in practice

> **In this part:** Ledgerline's invoice extraction moves out of the web process into a queue and a separate worker. Locally first, with an SQS-compatible server in Docker Compose: a worker killed mid-job, three workers sharing a batch, a poison message. Then on AWS: SQS, a worker service that scales from zero on queue depth, and alarms, with a minute-by-minute record of 30 invoices. For each: the steps, the real results, and what went wrong. Concepts are in [part 1](06-architecture-evolution.md).

Both were run in November 2026, on a Windows laptop (PowerShell 5, Docker Desktop) and in `ap-southeast-2` (Sydney). The problems described really happened.

---

## Contents

- [6B.1 What changed in the code](#6b1-what-changed-in-the-code)
- [6B.2 Demo 1: queue and worker, locally](#6b2-demo-1-queue-and-worker-locally)
- [6B.3 Demo 2: the worker on AWS](#6b3-demo-2-the-worker-on-aws)
- [6B.4 What the AWS demo showed](#6b4-what-the-aws-demo-showed)
- [6B.5 What went wrong](#6b5-what-went-wrong)
- [6B.6 Runbook: dead letters](#6b6-runbook-dead-letters)
- [6B.7 Troubleshooting](#6b7-troubleshooting)
- [6B.8 Checklist](#6b8-checklist)

---

## 6B.1 What changed in the code

Before this chapter, an upload saved the invoice and started extraction as a FastAPI background task in the same process. Now:

```
upload request ── save invoice, commit ── enqueue_extraction() ──> queue ──> worker ──> extract_invoice()
      │                                                              │
      └── 303 to the invoice page (tens of ms)                       └── 3 failures ──> dead-letter queue ──> alarm
```

| File | What it does |
|---|---|
| [`app/jobs.py`](../ledgerline/app/jobs.py) | The queue, from the app's side. `enqueue_extraction(invoice)` sends `{"type", "invoice_id", "version"}` plus the trace context as message attributes. `run()` executes one job. Two backends: `sqs` (deployed environments; ElasticMQ locally) and `inline` (tests, and running without Docker: the job runs immediately, through the same code path). Also `python -m app.jobs status` and `redrive`. |
| [`app/worker.py`](../ledgerline/app/worker.py) | The worker process: long-poll for one job, extend its visibility every half-period while it runs (the heartbeat), delete it only on success, leave it for a retry on any exception, log poison messages. Stops gracefully on `SIGTERM`. Touches a heartbeat file for its health check. Runs the stuck-extraction sweep every 5 minutes. |
| [`app/extraction.py`](../ledgerline/app/extraction.py) | `extract_invoice(id, expected_version, reclaim_after_seconds)`: skips a job whose invoice changed since it was queued (a duplicate, or superseded), and lets a *redelivered* job take over an invoice left in "extracting" by a dead worker. |
| [`app/workflow.py`](../ledgerline/app/workflow.py) | Two new transitions: `extracting → extracting` (a retry takes over) and `uploaded → extraction_failed` (the job never ran). |
| [`app/maintenance.py`](../ledgerline/app/maintenance.py) | The sweep now also fails invoices left "uploaded" for over an hour: the job was never queued (the app died between commit and send), or never started. |
| [`app/main.py`](../ledgerline/app/main.py) | Upload and re-extract enqueue after the commit. The startup recovery thread is gone: the API does no background work at all. |
| [`docker-compose.yml`](../ledgerline/docker-compose.yml) | Adds `elasticmq` (an SQS-compatible server) and `worker` (same image, `python -m app.worker`, `restart: unless-stopped`, a 2-minute stop grace period). Visibility timeout 30 s and a 10 s fake model delay, so the demo is quick to watch. |
| [`infra/modules/environment/jobs.tf`](../ledgerline/infra/modules/environment/jobs.tf) | The SQS queue (120 s visibility, 20 s long polling, encrypted) and its dead-letter queue (3 deliveries, 14 days' retention); alarms on dead letters and on the oldest waiting job (over 15 minutes). |
| [`infra/modules/environment/worker.tf`](../ledgerline/infra/modules/environment/worker.tf) | The worker's IAM role (read documents, consume jobs; nothing else), task definition (health check, 120 s stop timeout, the CloudWatch agent sidecar for traces), the ECS service without a load balancer, and step scaling on queue depth: +1 worker (or +2 for 20+ waiting) with a 3-minute cooldown; back to the minimum after 10 minutes with nothing waiting **or in progress**. |
| [`scripts/deploy_ecs.sh`](../ledgerline/scripts/deploy_ecs.sh) | Also rolls the worker service to the new image, without touching its task count (autoscaling owns that). |
| [`scripts/upload_many.py`](../ledgerline/scripts/upload_many.py) | Generates fresh synthetic invoices and uploads them through the real HTTP endpoint, to make work for the workers. |
| [`tests/test_jobs.py`](../ledgerline/tests/test_jobs.py) | Seven tests against moto's in-memory SQS: deleted only on success; duplicate delivery does the work once; a retry takes over from a dead worker; a first delivery never does; failures retried 3 times then dead-lettered; unreadable messages left for the dead-letter queue; the never-started sweep. |

Staging scales workers between 0 and 3, with a 15-second fake model delay so queueing is visible. Production keeps at least 1 worker (maximum 4), so the first job of the day doesn't wait for one to start.

---

## 6B.2 Demo 1: queue and worker, locally

**Cost:** free. About 20 minutes.

### Steps

From the `ledgerline` folder (the API is on port 8000; change the `ports:` line if 8000 is taken on your machine):

```powershell
# 1. Start everything: db, migrate, api, elasticmq, worker
docker compose up -d --build

# 2. Upload an invoice at http://localhost:8000 and watch the worker do the work
docker compose logs -f worker

# 3. Kill the worker in the middle of a job (upload, then while it says "extracting"):
docker compose kill worker
docker compose up -d worker
docker compose logs -f worker

# 4. A batch, three workers
docker compose up -d --scale worker=3
docker compose run --rm api python -m scripts.upload_many --count 20 --url http://api:8000
docker compose run --rm api python -m app.jobs status      # repeat every few seconds

# 5. A poison message
docker compose run --rm api python -c "from app import jobs; jobs.sqs_client().send_message(QueueUrl=jobs.queue_url(), MessageBody='not json')"
docker compose run --rm api python -m app.jobs status      # after about 2 minutes
```

### Results

**A normal job.** From upload to a worker starting it: **4–6 ms** (`job_waited_ms`). The extraction then took about 10.1 seconds (the fake model's 10-second delay), on the worker, while the upload request had long since returned.

**A worker killed mid-job.** The job's log lines, in order:

```
05:39:52  Job started  job_id b1ae9944…  job_attempt 1
          (docker compose kill worker; docker compose up -d worker)
05:40:08  Worker started
05:40:22  Job started  job_id b1ae9944…  job_attempt 2  job_waited_ms 30022
05:40:32  Extraction finished: needs_review
05:40:32  Job finished: done
```

The killed worker never deleted the message and stopped sending heartbeats, so 30 seconds (the visibility timeout) after it last did, SQS handed the job to the new worker. The invoice was still "extracting"; because this was a second delivery and the claim was older than the visibility timeout, the retry took it over. The invoice's audit trail records it: "Retry: the previous attempt stopped 30 s ago".

**A batch on three workers.** 20 uploads took **1.7 seconds** (83 ms each, the request no longer waiting for extraction). The queue, sampled every few seconds:

```
jobs  waiting 13   in progress 3
jobs  waiting  7   in progress 3
jobs  waiting  4   in progress 3
jobs  waiting  0   in progress 1
jobs  waiting  0   in progress 0
```

Twenty 10-second jobs, three at a time: about a minute. In an earlier run with a 5-second delay, **one worker drained 10 jobs in 52 seconds and three workers in 18**.

**A poison message.** The worker logged it as unreadable three times and never deleted it; on the fourth receive, ElasticMQ moved it to the dead-letter queue:

```
jobs          waiting     0   in progress     0
dead letters  waiting     1   in progress     0
```

### What went wrong locally

> **The worker crashed on its first start.** `Failed to resolve 'elasticmq'`: Compose started the worker before the queue server's container was ready, and the worker's first call (creating the queues) failed. `restart: unless-stopped` brought it back a few seconds later, and it worked. A process that depends on something should either retry its connection or be restarted by its supervisor; containers usually take the second approach, which is why every long-running service in a Compose file or a task definition should have a restart policy.

> **"Nothing runs on localhost:8007."** The new `docker-compose.yml` replaced a locally edited port mapping (8007 back to 8000). The app was fine; the logs said `Uvicorn running on http://0.0.0.0:8000`, inside the container. When a file you've customised is replaced, re-apply the customisation, or keep local changes in a `docker-compose.override.yml`, which Compose merges automatically.

---

## 6B.3 Demo 2: the worker on AWS

**Cost:** cents (SQS is inside the free tier; a few minutes of Fargate per worker). About an hour.

### Steps

The order matters: **Terraform first, then push.** The new app refuses to start in a deployed environment without the queue settings (`QUEUE_BACKEND=sqs` and `JOB_QUEUE_URL`), and those come from Terraform.

```powershell
# 1. Sign in; make sure the database is running
aws login --profile ledgerline
$Env:AWS_PROFILE = "ledgerline"
aws rds describe-db-instances --db-instance-identifier ledgerline-staging --query "DBInstances[0].DBInstanceStatus" --output text

# 2. Commit everything, and CHECK it's all in the commit
git add -A
git status --short          # nothing should be left as ?? or unstaged M
git commit -m "Chapter 6: job queue and worker"

# 3. Queues, worker service, scaling, alarms
cd ledgerline\infra\staging
terraform plan  -var alarms_enabled=false
terraform apply -var alarms_enabled=false

# 4. Push; wait for deploy-staging (migrate, roll the app, roll the worker)
git push
```

**A watch window** (a second PowerShell, from `ledgerline`), every 30 seconds:

```powershell
$q = terraform -chdir=infra/staging output -raw job_queue_url
while ($true) {
  $a = aws sqs get-queue-attributes --queue-url $q --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --query "Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]" --output text
  $s = aws ecs describe-services --cluster ledgerline-staging --services ledgerline-staging-worker --query "services[0].[desiredCount,runningCount]" --output text
  "{0}  jobs waiting/in progress: {1}   workers desired/running: {2}" -f (Get-Date -Format HH:mm:ss), $a, $s
  Start-Sleep 30
}
```

**Make work:** 30 fresh invoices, uploaded through the real endpoint. The site password goes from SSM into an environment variable and into the container, never onto the screen or the command line:

```powershell
$Env:SITE_PASSWORD = aws ssm get-parameter --name /ledgerline/staging/site_password --with-decryption --query Parameter.Value --output text
$url = terraform -chdir=infra/staging output -raw staging_url
docker compose run --rm -e SITE_PASSWORD api python -m scripts.upload_many --count 30 --url $url
```

**A poison message, then the dead-letter queue and its alarm:**

```powershell
aws sqs send-message --queue-url $q --message-body "not json" --query MessageId --output text
# about 10-25 minutes later (a worker starts from zero; 3 deliveries x 120 s visibility; the alarm's 5-minute period)
$dlq = terraform -chdir=infra/staging output -raw job_dead_letter_queue_url
aws sqs get-queue-attributes --queue-url $dlq --attribute-names ApproximateNumberOfMessages --query "Attributes.ApproximateNumberOfMessages" --output text
aws cloudwatch describe-alarms --alarm-names ledgerline-staging-jobs-dead-letters --query "MetricAlarms[0].StateValue" --output text
aws sqs purge-queue --queue-url $dlq
```

**Pause staging again** (the workers scale themselves to 0):

```powershell
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 0 --query "service.desiredCount"
aws rds stop-db-instance --db-instance-identifier ledgerline-staging --query "DBInstance.DBInstanceStatus" --output text
```

### Results: 30 invoices, minute by minute

```
23:15:12  jobs waiting/in progress: 0   0   workers desired/running: 0  0
23:15:47  jobs waiting/in progress: 30  0   workers desired/running: 0  0    <- uploaded
23:18:09  jobs waiting/in progress: 30  0   workers desired/running: 0  0
23:18:45  jobs waiting/in progress: 30  0   workers desired/running: 2  0    <- scaling: +2 (20 or more waiting)
23:19:20  jobs waiting/in progress: 29  1   workers desired/running: 2  2    <- first job starts
23:19:56  jobs waiting/in progress: 24  2   workers desired/running: 2  2
23:20:31  jobs waiting/in progress: 19  2   workers desired/running: 2  2
23:21:06  jobs waiting/in progress: 22  2   workers desired/running: 2  2    <- "waiting" went UP
23:21:42  jobs waiting/in progress: 13  2   workers desired/running: 2  2
23:22:17  jobs waiting/in progress: 5   2   workers desired/running: 2  2
23:22:53  jobs waiting/in progress: 1   2   workers desired/running: 3  2    <- a third worker, too late
23:24:03  jobs waiting/in progress: 0   0   workers desired/running: 3  3    <- all done
23:35:15  jobs waiting/in progress: 0   0   workers desired/running: 3  3
23:35:50  jobs waiting/in progress: 0   0   workers desired/running: 0  0    <- scaled in
```

**The poison message** was in the dead-letter queue (`1`) when checked; the alarm read `OK`, `OK`, then `ALARM` a few minutes later, on its 5-minute evaluation.

---

## 6B.4 What the AWS demo showed

| Measure | Result | Why |
|---|---|---|
| Upload to first job starting | **about 3½ minutes** | Scaling from zero: ~3 minutes for queue metrics to reach CloudWatch and the alarm to fire, ~35 seconds for the task to start |
| First job to last | **about 4¾ minutes** for 30 jobs on 2 workers | ~15 s of fake model time per job, two at a time, plus overhead |
| Upload to all 30 done | **about 8½ minutes** | The sum: a backlog cleared without anyone watching |
| Queue empty to workers at zero | **about 12 minutes** | 10 one-minute periods with nothing waiting or in progress, plus metric delay |
| Cost while idle | nothing | No worker tasks between bursts |

1. **Scale to zero works, and costs latency.** A 3½-minute wait for the first job is fine for staging and for batch work. It isn't fine when a user is watching an invoice say "uploaded". That's why production's minimum is 1.
2. **Scaling runs on old data.** The +2 step fired once the alarm saw 30 waiting; three minutes later (the cooldown) it added a third worker when 1 job was left, on data that was a minute or two old. That worker never had anything to do. Scale-out overshoots; size the maximum for what you can afford to overshoot.
3. **Approximate counts are approximate.** "Waiting" went from 19 to 22 with nothing new being queued. SQS counts are estimates from a distributed system; use them for alarms and trends, not for exact logic.
4. **Scale-in is deliberately slow.** Three idle workers for 12 minutes cost little, and the rule (nothing waiting *and* nothing in progress) means scale-in never stops a worker part-way through a job.
5. **The dead-letter path works end to end.** Three failed deliveries, moved aside automatically, an alarm, and a purge. Nothing blocked the real jobs.

---

## 6B.5 What went wrong

Each of these happened during the AWS demo, in this order.

> **The database had restarted itself, again.** It had been stopped a week earlier; Amazon RDS starts a stopped instance after seven days (the same thing happened in Chapter 5). `start-db-instance` then failed with `InvalidDBInstanceState … not in one of the following statuses: stopped`, which only means "it's already running". For breaks longer than a week, destroy staging or stop it again weekly.

> **The first deploy's migration failed.** `Migration failed (exit code: 1; Essential container in task exited). The app was NOT updated.` The pipeline did its job: it stopped before touching the app. Re-running the failed job succeeded with no change, so the cause was timing (most likely the database still starting after its automatic restart). Its log had expired from the search window by the time we looked, which is the other lesson: when a deploy fails, read its logs straight away.

> **A 503 from the site.** The app service had been at 0 tasks since staging was paused. `aws ecs wait services-stable` returned before the load balancer's health checks had passed, so the first request still got a 503; a minute later, `/livez` returned 200. "Stable" in ECS terms isn't "healthy behind the load balancer".

> **The new code was never deployed.** Thirty uploads succeeded, and the queue stayed at `0 0` for 20 minutes. The diagnosis, step by step:
>
> 1. SQS's own metric, `NumberOfMessagesSent`, was **0**: the app wasn't sending anything.
> 2. The app's log showed `POST /invoices 303` followed by `Extraction finished` **with the same `request_id`**: extraction was still running inside the web process, the Chapter 5 behaviour.
> 3. `git status` showed every Chapter 6 file as modified (`M`) or untracked (`??`). The commit step had been skipped; CI had built and deployed the previous commit (`aea660b`, Chapter 5), now running with Chapter 6's Terraform settings.
>
> Committing and pushing fixed it. Lessons:
> - **Check what you're shipping:** `git status` before pushing, and the commit hash in the deploy log against the one you expect.
> - **Old code with new configuration can look healthy.** The old app ignored the new queue settings and kept working, so nothing failed; it just didn't do the new thing. A post-deploy check that exercises the new path (one test upload, then "did a message arrive?") would have caught it in a minute.
> - **Measure from the other side.** The queue's own metrics settled in one command what twenty minutes of log searching hadn't.

> **Searching CloudWatch Logs is fiddly.** Along the way: `describe-log-streams` refuses `--order-by LastEventTime` together with a name prefix; `filter-log-events` with a time window found nothing because the window was too short; filter patterns match whole words (`queue` doesn't match `queued`). What worked every time: list the most recent streams (`describe-log-streams --order-by LastEventTime --descending`, no prefix), then read one with `get-log-events`.

---

## 6B.6 Runbook: dead letters

**Alarm:** `ledgerline-<env>-jobs-dead-letters`: jobs failed three times and were moved to the dead-letter queue.

1. **How many, and since when?**
   ```powershell
   aws sqs get-queue-attributes --queue-url $dlq --attribute-names ApproximateNumberOfMessages --output text
   ```
2. **Look at one without removing it** (it becomes visible again after the timeout):
   ```powershell
   aws sqs receive-message --queue-url $dlq --visibility-timeout 30 --query "Messages[0].Body" --output text
   ```
   A readable `{"type": "extract_invoice", "invoice_id": …}` means the job itself kept failing; anything else is a poison message.
3. **Find why:** search the worker's logs for `job_failed` or `job_poison` around the time (the log lines carry `job_id` and `job_attempt`), and the invoice's own history in the app.
4. **Fix the cause** (a bug, a missing document, a dependency that was down).
5. **Then either:**
   - **redrive**: move the messages back to the job queue, to be processed again:
     ```powershell
     $dlqArn = aws sqs get-queue-attributes --queue-url $dlq --attribute-names QueueArn --query "Attributes.QueueArn" --output text
     aws sqs start-message-move-task --source-arn $dlqArn
     ```
     (Locally: `docker compose run --rm api python -m app.jobs redrive`.)
   - or **purge** messages that can never succeed (`aws sqs purge-queue --queue-url $dlq`), after noting which invoices they were for. The stuck-extraction sweep will mark those invoices failed, and a person can re-extract them from the invoice page.

---

## 6B.7 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Worker log: `Failed to resolve 'elasticmq'` / `Could not connect to the endpoint URL` at start-up | The worker started before ElasticMQ | It restarts by itself (`restart: unless-stopped`); check `docker compose ps` |
| The app isn't on the port you expect | `docker-compose.yml` was replaced, and with it your port mapping | Re-apply it, or keep it in `docker-compose.override.yml` |
| Invoices stay "uploaded"; worker log shows nothing | No worker running, or it can't reach the queue | `docker compose ps`; `python -m app.jobs status`; in AWS, the worker service's events |
| Invoices stay "uploaded" in AWS; the queue's `NumberOfMessagesSent` is 0 | The app isn't sending: old code deployed, or `QUEUE_BACKEND` isn't `sqs` | Check the deploy's commit; the app's logs for `Job queued` or `Could not queue extraction` |
| App won't start: `Deployed environments must use QUEUE_BACKEND=sqs` | New code deployed before Terraform added the queue settings | `terraform apply`, then re-run the deploy |
| A killed job never comes back | The visibility timeout hasn't passed yet (30 s locally, 120 s in AWS), or the message was deleted | Wait; check `in progress` in `app.jobs status` |
| The same invoice was extracted twice | Two deliveries while the first attempt was still running past the visibility timeout without heartbeats | Check the worker's heartbeat warnings; the job's version check prevents most duplicates |
| Workers stay at 0 with jobs waiting | Scale-out alarm hasn't fired yet (allow ~3 minutes), or the worker can't start | `describe-alarms` for `…-worker-scale-out`; the worker service's events |
| Workers don't scale in | Something is still in progress, or the scale-in alarm needs 10 minutes of zero | Wait; check `ApproximateNumberOfMessagesNotVisible` |
| `upload_many`: `503 Service Temporarily Unavailable` | The app service is at 0, or not yet healthy behind the load balancer | Set its desired count to 1; wait a minute after `services-stable` |
| Dead-letter alarm stays `OK` with a message in the queue | The alarm evaluates 5-minute periods | Wait up to 5–10 minutes |
| `start-db-instance`: `InvalidDBInstanceState … not in … stopped` | The database is already running (often auto-started after 7 days) | Nothing to do |

---

## 6B.8 Checklist

You've seen the architecture evolve when you can:

- [ ] Show an upload returning in milliseconds while extraction happens on a worker
- [ ] Kill a worker mid-job and show the job retried and the record recovered
- [ ] Show a duplicate delivery doing its work once (the tests do; explain how)
- [ ] Run several workers on one queue and show the batch finishing faster
- [ ] Send a poison message and find it in the dead-letter queue, with its alarm
- [ ] Explain the visibility timeout, the heartbeat and why the retry may take over "extracting"
- [ ] Show workers scaling from zero on queue depth, and back, and explain the delays
- [ ] Say what scale-to-zero costs in latency, and why production keeps one worker
- [ ] Redrive or purge a dead-letter queue, following the runbook
- [ ] Explain the gap between "saved" and "queued", how Ledgerline covers it, and when you'd use an outbox instead

---

**Next:** Chapter 7, Auth, identity and multi-tenancy: real sign-in instead of the development login, single sign-on for enterprise customers, roles and permissions, and making sure one tenant can never see another's data.
