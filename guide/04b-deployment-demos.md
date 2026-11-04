# Chapter 4, part 2: Deployment models in practice

> **In this part:** the same Ledgerline container image, run four more ways: on Kubernetes (locally, with kind), on a single server, on AWS Lambda, and in a production environment built from the same Terraform as staging, with a pipeline that promotes the tested image after a human approves. For each: what it takes, what it shows, what went wrong, and what it costs. Concepts are in [part 1](04-deployment-models.md).

All four were built and run in October and November 2026. The problems in each section really happened.

---

## Contents

- [4B.1 One image, many platforms](#4b1-one-image-many-platforms)
- [4B.2 Kubernetes, locally with kind](#4b2-kubernetes-locally-with-kind)
- [4B.3 A single server](#4b3-a-single-server)
- [4B.4 Serverless: AWS Lambda](#4b4-serverless-aws-lambda)
- [4B.5 Production, and promoting a release](#4b5-production-and-promoting-a-release)
- [4B.6 What the demos showed](#4b6-what-the-demos-showed)
- [4B.7 Troubleshooting](#4b7-troubleshooting)
- [4B.8 Checklist](#4b8-checklist)

---

## 4B.1 One image, many platforms

The application code didn't change for any of these platforms. Only the way each one supplies configuration, runs migrations and checks health did:

| | Docker Compose (local) | ECS on Fargate (staging, production) | Kubernetes | Single VM | Lambda |
|---|---|---|---|---|---|
| Image | `dev` target | `runtime`, from ECR | `runtime`, loaded into the cluster | `runtime`, pulled from ECR | Same code as a zip, plus an adapter |
| Settings | `environment:` in Compose | Task definition `environment` | ConfigMap | `.env` generated on the server | Function environment variables |
| Secrets | `.env` (local only) | Parameter Store, injected at start | Secret | Generated on the server, never leave it | Function environment (demo shortcut) |
| Migrations | `migrate` service, runs first | One-off task before each rollout | Job | `migrate` service | None: tables created on start (demo only) |
| Health | Compose healthcheck | Container `/livez`, load balancer `/readyz` | Startup, liveness and readiness probes | Compose healthcheck | Adapter's readiness check on `/livez` |
| HTTPS | — | CloudFront | (port-forward locally) | Caddy + Let's Encrypt | API Gateway |
| Files | Volume | S3 | Volume (demo) | Volume | `/tmp` (lost) |

This is part 1's portability table ([4.9](04-deployment-models.md#49-moving-between-models)) in practice: one image, configuration from the environment, migrations as a separate step, health endpoints. Each platform has its own way of doing each step; the app doesn't care which.

---

## 4B.2 Kubernetes, locally with kind

**kind** ("Kubernetes in Docker") runs a real Kubernetes cluster as containers on your machine. It's free and fast to create and delete, and it's what the Kubernetes project uses to test itself. It's not for production; it's for learning and testing manifests.

### The objects, mapped to what you already know

| Kubernetes | What it is | Ledgerline's equivalent elsewhere |
|---|---|---|
| **Pod** | One or more containers scheduled together | An ECS task |
| **Deployment** | "Keep N identical pods running; replace them gradually on change" | An ECS service |
| **Service** | A stable name and address in front of pods | The load balancer's target group |
| **StatefulSet** | Pods with stable names and their own disks | (A database server) |
| **Job** | Run a pod to completion | The migration task |
| **ConfigMap** / **Secret** | Settings / sensitive settings, injected as environment variables | Task definition `environment` / `secrets` |
| **PersistentVolumeClaim** | "Give me a disk" | An EBS volume |
| **Probes** | Startup, liveness and readiness checks | Container health check, load balancer health check |
| **Namespace** | A named group of objects | (A separate cluster or account, loosely) |

### The manifests

In [`ledgerline/deploy/kubernetes/`](../ledgerline/deploy/kubernetes/), applied together with **Kustomize** (`kubectl apply -k`), which is built into kubectl:

| File | Contents |
|---|---|
| `config.yaml` | The ConfigMap: the same variables ECS and Compose set |
| `postgres.yaml` | A StatefulSet and Service for Postgres, with a 1 Gi volume (demo only; section below) |
| `migrate-job.yaml` | The migration Job: `alembic upgrade head && python -m app.db_roles && python -m app.seed` |
| `app.yaml` | The app's Deployment, Service and a volume for uploads |
| `kustomization.yaml` | Lists the files and the namespace; the setup commands are in its comments |

The parts of `app.yaml` worth reading closely:

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0        # never go below the desired count during a rollout
    maxSurge: 1              # start one new pod before stopping an old one
...
startupProbe:                # give a slow start time before liveness applies
  httpGet: { path: /livez, port: 8000 }
  periodSeconds: 2
  failureThreshold: 30
livenessProbe:               # "restart me": the process is stuck
  httpGet: { path: /livez, port: 8000 }
readinessProbe:              # "don't send me traffic": can't reach the database
  httpGet: { path: /readyz, port: 8000 }
resources:
  requests: { cpu: 100m, memory: 256Mi }   # what the scheduler reserves
  limits: { memory: 512Mi }                # the ceiling; no CPU limit (throttling hurts latency)
```

Three details that each cost someone a debugging session:

- **The Secret is created by hand**, not from a file in the repository: `kubectl create secret generic ledgerline-db --from-literal=…`. In a real cluster, secrets come from the cloud's secrets manager (External Secrets Operator or the Secrets Store CSI driver), never from YAML in git. Kubernetes Secrets are only base64-encoded, not encrypted, unless the cluster is configured to encrypt them.
- **kind can't see your local Docker images.** `kind load docker-image ledgerline:local` copies the image into the cluster's node. Forget it, and pods sit in `ErrImagePull`.
- **A new volume is owned by root**, and the app runs as user 1000. An init container fixes ownership before the app starts. This is the same problem as Compose's named volumes, and the same fix the single-server demo uses.

### Running it

```powershell
winget install Kubernetes.kind
winget install Kubernetes.kubectl          # reopen PowerShell afterwards
kind create cluster --name ledgerline

cd ledgerline
docker build --target runtime -t ledgerline:local .
kind load docker-image ledgerline:local --name ledgerline

kubectl create namespace ledgerline
kubectl -n ledgerline create secret generic ledgerline-db --from-literal=owner-password=dev-owner-pw --from-literal=app-password=dev-app-pw
kubectl apply -k deploy/kubernetes
kubectl -n ledgerline get pods -w
kubectl -n ledgerline port-forward svc/ledgerline 8009:80      # http://localhost:8009
```

### What to try

| Command | What it shows |
|---|---|
| `kubectl -n ledgerline delete pod -l app=ledgerline` | The Deployment replaces the pod within seconds: self-healing |
| `kubectl -n ledgerline rollout restart deployment/ledgerline` | A rolling restart: the new pod becomes ready before the old one stops |
| `kubectl -n ledgerline scale statefulset postgres --replicas=0` | The app pod goes to `READY 0/1` but is **not restarted**: readiness fails, liveness doesn't ([Chapter 2, section 2.8](02-production-readiness.md#28-health-checks-shutdowns-and-interrupted-work)) |
| `kubectl -n ledgerline get events --sort-by=.lastTimestamp` | Every scheduling, pulling, probing and restarting decision, in order |

### What this demo deliberately doesn't show

- **Getting traffic in.** Locally, `port-forward` is enough. In a cluster, traffic arrives through a **Gateway** (the Gateway API, Kubernetes' standard for this; version 1.4 was released in October 2025) or an Ingress controller. The most widely used one, **ingress-nginx, reached end of life in March 2026**: no more updates or security fixes. New clusters should use a Gateway API implementation (several are available, including from the cloud providers and from Traefik, Envoy-based projects and others).
- **A production database.** Databases are the hardest thing to run well on Kubernetes. Use a managed database outside the cluster (RDS, Cloud SQL), or a mature operator (CloudNativePG) with backups configured and tested.
- **Files.** A volume ties a pod to one node. In a real cluster, uploads go to S3 (`STORAGE_BACKEND=s3`), as they do on ECS.
- **Upgrades, autoscaling, policies, observability agents.** The ongoing work of running a cluster (part 1, [4.5](04-deployment-models.md#45-kubernetes-when-its-worth-it)).

Clean up with `kind delete cluster --name ledgerline`.

---

## 4B.3 A single server

The cheapest real deployment: one EC2 instance running Docker Compose. Everything is in [`ledgerline/infra/vm-demo/`](../ledgerline/infra/vm-demo/) and [`ledgerline/deploy/vm/`](../ledgerline/deploy/vm/).

```
browser ──HTTPS──▶ Caddy ──▶ app ──▶ Postgres        all on one t3.small, one disk, one AZ
                (Let's Encrypt)
```

### How it's built

- **Terraform** creates the server, a firewall allowing only ports 80 and 443, and an IAM role.
- **cloud-init** runs once, on first boot: installs Docker, Compose and the ECR credential helper, writes the Compose file and the Caddyfile, generates secrets, and starts everything.
- **The image** is pulled from ECR using the server's IAM role, through `amazon-ecr-credential-helper`. No AWS keys on the server.
- **HTTPS** comes from **Caddy**, a web server that obtains and renews Let's Encrypt certificates automatically. Certificates need a hostname, and we have no domain, so the server uses **sslip.io**: a free DNS service where `13-55-168-182.sslip.io` resolves to `13.55.168.182`.
- **Secrets are generated on the server** (`openssl rand`) into a root-only `.env`. They never pass through Terraform state, user data, or git.
- **No SSH.** Port 22 is closed and there's no key pair. Commands run through **AWS Systems Manager**: authenticated by your AWS login, logged in CloudTrail, nothing exposed to the internet.

```powershell
# a helper: run one command on the server and print the output
function vm($command) {
  $c = aws ssm send-command --instance-ids $id --document-name AWS-RunShellScript --parameters commands="$command" --query Command.CommandId --output text @aws
  do { Start-Sleep 2; $r = aws ssm get-command-invocation --command-id $c --instance-id $id --output json @aws | ConvertFrom-Json } while ($r.Status -in 'Pending','InProgress')
  $r.StandardOutputContent; $r.StandardErrorContent
}
vm "docker compose -f /opt/ledgerline/compose.yaml ps -a"
```

### What you own on a single server

| Job | Here | On a platform |
|---|---|---|
| OS security patches | `unattended-upgrades`, set up by cloud-init; reboots are still yours | The provider |
| Disk space | `df -h /`; container logs are capped at 3 × 10 MB each in `compose.yaml` (unlimited logs filling the disk is a classic single-server outage) | The provider |
| Database backups | **None.** You'd add a `pg_dump` to S3 on a schedule, and test restores (Chapter 2) | Automated snapshots, point-in-time recovery |
| Deploys | `docker compose pull && docker compose up -d`: seconds of downtime | Rolling, no downtime |
| Failure of the machine or its AZ | Everything is down until you rebuild it | Replaced automatically, other AZs keep serving |

The `restart api` experiment makes the last two rows visible: for a few seconds, Caddy returns 502, because there's no second copy of the app to take the traffic.

### What went wrong, and what it teaches

> **One forbidden character, one broken server.** The first `apply` failed with *"Invalid rule description"*: two firewall rule descriptions contained an apostrophe ("Let's Encrypt"), which EC2 doesn't allow in that field. Terraform had already created the server, though, and the server booted **without its outbound rule**. cloud-init's log showed `Network is unreachable` for every package download, then `docker: not found`.
>
> Fixing the descriptions and applying again added the missing rules, but cloud-init only runs on first boot, so the server stayed broken. The fix was to **replace** it: `terraform apply "-replace=aws_instance.vm"`. Lessons:
> - A failed apply can leave resources that **exist but were set up wrong**. Read the errors, then check what was actually created.
> - First-boot setup is one-shot. Treat servers as **cattle, not pets**: when the setup is wrong, replace the server rather than repairing it by hand.
> - Read the boot log (`/var/log/cloud-init-output.log`) before guessing.

Two PowerShell traps, again: `-replace=aws_instance.vm` must be quoted (PowerShell splits it at the `.`), and `terraform destroy` must run in the folder that has the Terraform files. From the repository root, Terraform reports *"Value for undeclared variable"*, because it finds no configuration there and so no variable declarations.

**Cost:** a t3.small in Sydney is about $0.70 a day (on the new Free plan it's an eligible type, drawn from the credits), plus a few cents for the disk and public IP.

---

## 4B.4 Serverless: AWS Lambda

The same FastAPI app on Lambda, behind an API Gateway HTTP API. Files: [`ledgerline/deploy/lambda/`](../ledgerline/deploy/lambda/) and [`ledgerline/infra/lambda-demo/`](../ledgerline/infra/lambda-demo/).

```
browser ──▶ API Gateway (HTTP API) ──▶ Lambda: Lambda Web Adapter ──HTTP──▶ uvicorn ──▶ the same FastAPI app
```

### Running an ordinary web app on Lambda

The **Lambda Web Adapter** is a small extension, published by AWS as a public layer. Lambda runs the function's handler (`run.sh`), which starts uvicorn as usual; the adapter waits until the app answers its readiness check, then turns every Lambda invocation into a plain HTTP request to `localhost`. The app has no idea it's on Lambda. The alternative, an in-process adapter library such as Mangum, wraps the ASGI app instead; both work.

```hcl
handler = "run.sh"
layers  = ["arn:aws:lambda:${var.region}:753240598075:layer:LambdaAdapterLayerX86:30"]
environment {
  variables = {
    AWS_LAMBDA_EXEC_WRAPPER      = "/opt/bootstrap"   # hand start-up to the adapter
    AWS_LWA_PORT                 = "8000"
    AWS_LWA_READINESS_CHECK_PATH = "/livez"
    ...
  }
}
```

### Packaging, and two Windows traps

The package is built inside a Linux container (`python:3.12-slim`), so it's identical whether you build on Windows, macOS or Linux:

```powershell
docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim sh -c "sed 's/\r$//' deploy/lambda/build.sh | sh"
```

`pip install --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12` downloads Linux wheels for Lambda's Python 3.12 runtime: 123 MB unzipped, a 39 MB zip, within Lambda's 250 MB unzipped limit. Over 50 MB zipped, it would have to go through S3; Terraform uploads it through S3 anyway.

- **Line endings.** A shell script with Windows line endings fails on Linux with confusing errors. `build.sh` strips them, the repository's `.gitattributes` keeps `*.sh` files LF, and the `sed` in the command above protects the build script itself.
- **The executable bit.** Zipping on Windows (including Terraform's `archive_file`) loses Unix file permissions, and Lambda then can't run `run.sh`. So the build script creates the zip inside Linux, where `run.sh` keeps mode `0755`, and Terraform uploads that file as it is.

### Why API Gateway rather than a function URL

Lambda function URLs are the simplest way to give a function an HTTPS address. Since October 2025, a public function URL (`AuthType: NONE`) needs **two** permissions in the function's policy: `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`, the second limited with the condition `lambda:InvokedViaFunctionUrl`. Without that condition, anyone could invoke the function directly through the Lambda API. We couldn't confirm that the Terraform AWS provider supported the condition yet, and the unconditional version is unsafe, so the demo uses an API Gateway HTTP API instead, whose permissions are well established. Two more reasons:

- **Throttling** on the stage (10 requests per second, bursts of 20) caps what a script hammering a public, pay-per-request URL can cost.
- API Gateway is what most production Lambda APIs use anyway (custom domains, authorisers, usage plans).

### What you should see, and why

The demo sets `FAKE_LLM_DELAY_SECONDS=10` so extraction takes as long as a real model would.

| Behaviour | Why | The real fix |
|---|---|---|
| **The first request takes seconds** | A cold start: Lambda starts Python, `run.sh` creates the tables and seeds users, uvicorn starts, the adapter waits for readiness | Smaller packages, no work at start-up, provisioned concurrency |
| **Uploaded invoices stay "extracting"** | Lambda **freezes the instance as soon as the response is sent**. FastAPI's background task, still running, is paused mid-way, and may resume during a later request, or never | Hand background work to a queue and a separate consumer (Chapter 6), or a workflow service |
| **Data disappears after a while** | The database is SQLite in `/tmp`: each Lambda instance has its own, and it vanishes when the instance is recycled. Two concurrent instances don't even share it | An external database (RDS through RDS Proxy, or a serverless-friendly database) and S3 for files |

None of these are bugs in Lambda. They're the model: stateless, per-request, frozen between requests. Code written for a long-running server has to be rearranged to fit.

**Cost** at demo traffic: effectively zero. Lambda's free tier is 1 million requests and 400,000 GB-seconds a month; API Gateway HTTP APIs cost about $1 per million requests.

---

## 4B.5 Production, and promoting a release

The largest demo: a production environment built from **the same Terraform as staging**, and a pipeline that deploys the image staging tested to production after a person approves.

### Step 1: staging's code becomes a module

Staging's resources (network rules, database, secrets, app, observability) moved from `infra/staging/*.tf` into [`infra/modules/environment/`](../ledgerline/infra/modules/environment/). Differences between environments became inputs:

| Input | Staging | Production |
|---|---|---|
| `vpc_id`, subnets | The default VPC's public subnets | Its own VPC: load balancer in public subnets, app and database in private subnets |
| `assign_public_ip` | `true` (no NAT) | `false` (outbound through NAT) |
| `multi_az` | `false` | `true` |
| `llm_provider`, `dev_login`, `seed_demo_data` | `fake`, `true`, `true` | `gemini`, `false`, `false` |

Each environment's root folder is now small: providers, the network, one `module "env"` call, and its CI identity.

**Moving resources without rebuilding them.** Terraform identifies a resource by its address. `aws_db_instance.main` becoming `module.env.aws_db_instance.main` looks, to Terraform, like "delete the old database, create a new empty one", unless you say otherwise. `moved` blocks say otherwise:

```hcl
moved {
  from = aws_db_instance.main
  to   = module.env.aws_db_instance.main
}
```

[`staging/moved.tf`](../ledgerline/infra/staging/moved.tf) has 55 of these, generated from the list of resources that moved. The plan after the refactor:

```
# aws_db_instance.main has moved to module.env.aws_db_instance.main
...
Plan: 0 to add, 15 to change, 0 to destroy.
```

**0 to add, 0 to destroy**: every resource was recognised, including the database. Of the 15 changes, 6 were expected (five saved queries renamed to include `/Staging/`, the canary's zip path). The other 9 were a different lesson.

> **Drift.** All 9 were `actions_enabled = false -> true` on the alarms. When staging was paused (Chapter 3, part 2), the alarm notifications were turned off **with the CLI**. Terraform's code still said "on", so the plan would have switched them back on while staging was paused, and the alarms would have emailed about a site that was down on purpose. The real system had *drifted* from the code. The fix was to apply with the paused value, `terraform apply -var alarms_enabled=false`, and from then on to change it through Terraform. Every plan is also a drift report: changes you didn't make in code are changes someone made outside it.

### Step 2: a network module

[`infra/modules/network/`](../ledgerline/infra/modules/network/main.tf) builds a VPC across two availability zones: public subnets for the load balancer and NAT gateway, private subnets for app tasks and the database, a free S3 gateway endpoint so S3 traffic skips NAT, and one NAT gateway (`nat_per_az = false`; a real production setup uses one per AZ, at about twice the NAT cost). Subnets are `/20` ranges cut from `10.20.0.0/16`, with production and staging given non-overlapping ranges so they could be connected later.

### Step 3: production's own CI identity

Production doesn't reuse staging's CI user:

- **Its permissions** come from the same module output, scoped to production's cluster, service, task families and roles. It can't push images: production only ever runs images that staging built and tested.
- **Its key lives in the GitHub `production` environment's secrets**, named `PRODUCTION_AWS_ACCESS_KEY_ID` and `PRODUCTION_AWS_SECRET_ACCESS_KEY`. Only jobs that use the `production` environment can read them, and that environment requires a reviewer's approval. Distinct names mean a missing environment secret makes the job fail, rather than silently fall back to staging's repository-wide credentials.

> **A plan limit decided the repository's visibility.** On GitHub's Free, Pro and Team plans, required reviewers (and other deployment protection rules) only work in **public** repositories, and environment secrets in private repositories need a paid plan. Ledgerline's repository became public; it's the guide's reference implementation anyway. Before doing that, check the whole history, not just the current files, for account IDs, URLs, keys and personal details. The commit author email is part of every commit and becomes public too; GitHub's "keep my email addresses private" setting and its `noreply` address protect future commits.

### Step 4: the promotion job

```yaml
deploy-production:
  needs: [build, deploy-staging]                  # only after staging deployed successfully
  if: needs.build.outputs.image != '' && vars.PRODUCTION_ECS_CLUSTER != ''
  environment:
    name: production                              # waits here for a reviewer's approval
    url: ${{ vars.PRODUCTION_URL }}
  steps:
    - uses: aws-actions/configure-aws-credentials@…
      with:
        aws-access-key-id: ${{ secrets.PRODUCTION_AWS_ACCESS_KEY_ID }}
        aws-secret-access-key: ${{ secrets.PRODUCTION_AWS_SECRET_ACCESS_KEY }}
        aws-region: ${{ vars.AWS_REGION }}
    - run: bash scripts/deploy_ecs.sh             # same script as staging: migrate, roll out, verify
      env:
        IMAGE: ${{ needs.build.outputs.image }}   # the SAME image staging is running
        CLUSTER: ${{ vars.PRODUCTION_ECS_CLUSTER }}
        DESIRED_COUNT: "2"                        # one task per availability zone
    - run: curl -fsS "${{ vars.PRODUCTION_URL }}/readyz"   # (with retries) the public address answers
```

In GitHub, the run pauses at *"Waiting for review"*; **Review deployments → production → Approve and deploy** releases it. Re-running only the failed job (**Re-run failed jobs**) reuses the earlier jobs' outputs, so it deploys the same image without rebuilding.

### What happened on the way to the first approved deploy

Three failures, each one a safeguard doing its job:

| Run | What failed | Cause | Fix |
|---|---|---|---|
| #9 | Deploy to staging: migration couldn't connect | Staging had been paused minutes earlier; the database was stopped | Resume staging first. The deploy stopped before touching the app, as designed |
| #10 | Secrets and dependencies, after 47 s | `pip-audit` found 4 CVEs **published that week** in `pyasn1` 0.6.4, an indirect dependency (through Google's auth library). The same code had passed days earlier | Upgrade just that package (`uv pip compile --upgrade-package pyasn1`) to 0.6.5; the audit passed. Chapter 8 covers automating this |
| #11 | Deploy to production: migration exited 1 | `RuntimeError: LLM_PROVIDER=fake is not allowed in production.` The app's own configuration check (Chapter 2) refused production settings that were wrong: the fake model, the demo login and demo seeding | Production uses `llm_provider = "gemini"`, `dev_login = false`, `seed_demo_data = false`; the migration task gets the same settings as the app, since the checks run there too |

After the fix, re-running the failed job (with a new approval) migrated and rolled out two tasks in private subnets. Production's `/readyz` returned `{"status":"ok","database":"ok"}`, and the site's pages answered:

```json
{"detail":"Authentication is not configured (see Chapter 7)."}
```

That's the honest state of Ledgerline's production: the platform, the network and the pipeline are production-grade, and the application **correctly refuses to run a demo login in production**. It can serve real users once it has real authentication, which is Chapter 7. Before the fix, production's URL returned the load balancer's **503 Service Temporarily Unavailable**: there were no healthy targets because the migration never succeeded, so the app was never started.

### Tearing it down

Production costs roughly $5 a day (NAT gateway, Multi-AZ database, load balancer, two tasks). In order:

```powershell
# 1. Revoke CI access first: an IAM user with an access key can't be deleted,
#    and a key Terraform didn't create, Terraform won't delete
aws iam list-access-keys --user-name ledgerline-production-github-ci @aws
aws iam delete-access-key --user-name ledgerline-production-github-ci --access-key-id <id> @aws
gh secret delete PRODUCTION_AWS_ACCESS_KEY_ID --env production
gh secret delete PRODUCTION_AWS_SECRET_ACCESS_KEY --env production
gh variable delete PRODUCTION_ECS_CLUSTER        # future pushes skip the production job
gh variable delete PRODUCTION_URL

# 2. Destroy (the Multi-AZ database and CloudFront take the longest)
cd ledgerline/infra/production
terraform destroy

# 3. The final database snapshot survives destroy by design; delete it if the data doesn't matter
aws rds delete-db-snapshot --db-snapshot-identifier ledgerline-production-final @aws
```

Cancel any pipeline run that's waiting for production approval first, so it can't deploy into an environment being destroyed. Keep the GitHub `production` environment and its reviewer rule: they cost nothing.

---

## 4B.6 What the demos showed

| | Time to a working deploy | Cost while running | Deploy downtime | What we had to handle ourselves |
|---|---|---|---|---|
| Kubernetes (kind) | ~15 min, once tools are installed | Free (your laptop) | None (rolling) | Secrets, volumes and permissions, image loading; in a real cluster, far more |
| Single VM | ~10 min, plus one replacement | ~$0.70/day | Seconds | Patching, disk, backups (none), certificates (Caddy did it), recovery |
| Lambda | ~10 min | ~$0 at demo traffic | None | Packaging for Linux, cold starts, **background work and state** (didn't fit) |
| ECS staging (Chapters 2–3) | Hours, the first time | ~$2.10/day | None (rolling) | Infrastructure as code for every piece; little ongoing work |
| ECS production | ~30 min, reusing staging's module | ~$5/day | None (rolling, two AZs) | NAT and network design, a separate CI identity, approvals |

The pattern: the cheaper and simpler the platform, the more of the reliability work is yours, or simply missing. The more managed the platform, the more it costs at low traffic and the more there is to set up once. And the application's design (background work, state) decides which platforms are even possible.

---

## 4B.7 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pods in `ErrImagePull` on kind | The image wasn't loaded into the cluster | `kind load docker-image ledgerline:local --name ledgerline` |
| `InvalidParameterValue: Invalid rule description` creating a security group rule | An apostrophe (or other disallowed character) in the description | Use only `a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*` |
| VM: `docker: not found` in the cloud-init log | The server booted without outbound access (rules created later) | Replace the instance: `terraform apply "-replace=aws_instance.vm"` |
| `Invalid force-replace address "aws_instance"` | PowerShell split the argument at `.` | Quote it: `"-replace=aws_instance.vm"` |
| `Value for undeclared variable` on `terraform destroy` | Run outside the Terraform folder | `cd` into the folder with the `.tf` files |
| Lambda: `permission denied` running `run.sh` | Zipped on Windows: the executable bit was lost | Build and zip inside Linux (`build.sh` does) |
| Lambda: invoices stuck in "extracting" | Background work frozen after the response | Expected on Lambda; use a queue and worker (Chapter 6) |
| `terraform plan` after a refactor wants to destroy things | Missing `moved` blocks | Add `moved { from = … to = … }` for every address that changed; plan until it shows 0 to destroy |
| Unexpected changes in a plan you didn't make in code | Drift: someone changed the real system (CLI, console) | Bring the code in line (as with `alarms_enabled`), or let Terraform revert it, deliberately |
| CI fails the dependency audit with no code change | New CVEs published for a dependency | Upgrade the affected package; automate with Dependabot or Renovate (Chapter 8) |
| Production deploy: `LLM_PROVIDER=fake is not allowed in production` | The app's configuration check refusing unsafe production settings | Production values: real provider, no demo login, no demo seed |
| Production URL: 503 from the load balancer | No healthy tasks: usually the deploy never got past migration | Read the deploy job's migration output |
| *"Waiting for review"* never appears; job runs or is skipped | The repository is private on a free plan, or the environment has no reviewers | Public repository, or a paid plan; add a required reviewer to the environment |

---

## 4B.8 Checklist

You've seen the deployment models in practice when you can:

- [ ] Run the same image on at least two platforms without changing application code
- [ ] Explain, for each platform you use, where configuration, secrets, migrations and health checks come from
- [ ] Name what your platform does for you, and what's still yours (patching, backups, disk, recovery)
- [ ] Say which parts of your application wouldn't fit serverless functions, and why
- [ ] Refactor infrastructure code without rebuilding what it manages (`moved` blocks; a plan with 0 to destroy)
- [ ] Recognise drift in a plan, and decide deliberately whether code or reality is right
- [ ] Build production from the same modules as staging, with different values
- [ ] Promote the exact image that passed staging to production, after an approval, with credentials only that step can use
- [ ] Tear an environment down completely, including keys, CI variables and snapshots

---

**Next:** [Chapter 5, The data layer](05-data-layer.md): Postgres in production, backups and restores at scale, read replicas, caching, search, and the data that lives outside the database.
