# Chapter 4: Deployment models and infrastructure

> **In this chapter:** where your software runs, and how that choice changes as a product grows. It covers the five deployment models (platform as a service, a single server, managed containers, Kubernetes and serverless functions): what each asks of you, what each costs to run and to operate, and when each is the right call. Then production-grade networking, separate environments and accounts, promoting a release from staging to production, and moving between models without a rewrite.
>
> **Part 2, [Deployment models in practice](04b-deployment-demos.md),** runs Ledgerline on a single server, on Kubernetes and on serverless functions, then builds a production environment on AWS with a promotion pipeline.

---

## 4.1 Why it matters

Every deployment model answers one question differently: **who operates what?**

At one end, a platform as a service runs almost everything; you push code. At the other, Kubernetes on your own servers gives you control of everything, and the job of operating everything. The choice decides:

- **How much engineering time goes into infrastructure** instead of the product. This is usually the biggest cost, and it never appears on the cloud bill.
- **The cost floor**: what you pay with zero traffic. It ranges from nothing to hundreds of dollars a month.
- **What's possible**: long-running background work, WebSockets, GPUs, private networking, compliance controls.
- **How hard it is to leave**, if prices, limits or needs change.

The good news: this choice is more reversible than most, *if* you keep the application portable. Ledgerline has moved from `docker compose` on a laptop to ECS on Fargate without changing a line of application code, because the app follows a few rules (section 4.9). This chapter moves it to three more models the same way.

> **Pick the model that lets you spend the least time on infrastructure for the next 12 to 18 months. Revisit it when its limits, not its fashionability, start to cost you.**

---

## 4.2 The five models

```
                     you manage more  ◀──────────────────────────────────▶  provider manages more

  Single VM          Kubernetes          Managed containers        PaaS              Serverless functions
  (EC2, Hetzner,     (EKS, GKE, AKS,     (ECS/Fargate, ECS         (Render, Railway, (Lambda, Cloud Run
   DigitalOcean)      self-managed)       Express, Cloud Run,       Fly.io, Heroku)   functions, Azure
                                          Azure Container Apps)                       Functions)
```

### Platform as a service (PaaS)

You connect a repository or push an image; the platform builds, runs, scales, adds HTTPS and often provides a database. Examples: **Render, Railway, Fly.io, Heroku**, and the "app" products of the big clouds.

- **You manage:** the app, its configuration, and choosing plan sizes.
- **Great for:** MVPs and early production; small teams; anything where "deploy on push, HTTPS included" is worth more than control.
- **Watch for:** pricing that grows quickly past the small plans, limited networking (private connectivity to customer systems, fixed outbound IPs), and the provider's limits on background workers, disk and regions.

> **AWS App Runner is closed to new customers** (existing customers can continue). For new AWS projects, AWS recommends **ECS Express Mode** (launched November 2025): give it a container image and two IAM roles, and it creates the ECS service, Fargate tasks, a load balancer, scaling and a URL. No extra charge; you pay for the resources it creates. It's AWS's answer to "PaaS simplicity, but inside my account".

### Single virtual machine

One server (EC2, Lightsail, Hetzner, DigitalOcean, Linode…) running your containers with Docker Compose, plus a reverse proxy such as **Caddy** for automatic HTTPS.

- **You manage:** the operating system, patches, Docker, backups, disk space, monitoring, restarts after reboots, and the database if it runs there too.
- **Great for:** the cheapest real deployment; internal tools; predictable steady load; learning.
- **Watch for:** it's a single point of failure (one disk, one machine, one availability zone); deploys usually mean a few seconds of downtime; "who patched the server?" becomes a real question; a database on the same machine means *you* own backups and restores.

### Managed containers

You give the platform a container image and settings; it runs and replaces containers, spreads them across availability zones and connects them to a load balancer. No servers to patch. Examples: **AWS ECS on Fargate** (Chapters 2 and 3), **ECS Express Mode**, **Google Cloud Run**, **Azure Container Apps**.

- **You manage:** images, task or service definitions, networking and IAM (with infrastructure as code).
- **Great for:** early production through growth; most web apps and APIs; teams who want production-grade without running a cluster.
- **Watch for:** the cost floor (load balancer, NAT or public IPs) is higher than a VM; each cloud's version is its own product, so the definitions don't port directly, though the image does.

### Kubernetes

A standard API for running containers across a cluster of machines, with a huge ecosystem of add-ons. Managed control planes: **EKS** (AWS), **GKE** (Google), **AKS** (Azure); or run it yourself.

- **You manage:** the cluster (upgrades every few months, add-ons, node groups or autoscalers), manifests or Helm charts, ingress, certificates, secrets integration, policies, and the people who know all of it.
- **Great for:** many services and teams sharing one platform; workloads that need fine control (GPUs, sidecars, operators, custom scheduling); portability across clouds and on-premises; organisations with a platform team.
- **Watch for:** the operational load is real and continuous. EKS costs **$0.10 per hour per cluster** (~$73/month) before any nodes, and **$0.60/hour** if a cluster falls behind into extended support. Section 4.5 is about when it's worth it.

### Serverless functions

You upload a function; the platform runs it per request or event, scales to zero, and bills per invocation and millisecond. Examples: **AWS Lambda**, Google Cloud Run functions, Azure Functions. (Cloud Run itself sits between this and managed containers: containers that scale to zero.)

- **You manage:** the function code, its configuration, triggers and permissions.
- **Great for:** spiky or low traffic (pay nothing when idle); event handling (a file arrived, a message queued, a schedule); glue between services.
- **Watch for:** time limits (Lambda: 15 minutes), cold starts, stateless execution, database connection storms, local development that differs from production, and costs that cross over to *more* expensive than containers at sustained high load. Section 4.6 goes through these with Ledgerline's own background-work problem.

---

## 4.3 Side by side

| | PaaS | Single VM | Managed containers | Kubernetes | Serverless functions |
|---|---|---|---|---|---|
| Ops effort | Lowest | Medium, all yours | Low–medium | Highest | Low |
| Cost floor (idle) | ~$0–25 | ~$5–20 | ~$40–70 with a load balancer | ~$75+ (EKS) before nodes | ~$0 |
| Scaling | Built in, within plan limits | Manual (bigger VM) | Autoscaling | Autoscaling, most flexible | Automatic, per request |
| Zero-downtime deploys | Usually | Not without extra work | Yes (rolling) | Yes | Yes |
| Long-running background work | Worker services | Yes | Yes | Yes | Limited (15 min; needs redesign) |
| WebSockets, streaming | Usually | Yes | Yes | Yes | Limited |
| Private networking, compliance controls | Limited | Yes (you build it) | Yes | Yes | Yes |
| Portability of the definitions | Low | High | Medium (the image ports) | High | Low |
| Local development matches production | Close | Very close (same Compose) | Close | Close (kind, minikube) | Furthest |

The **image** is the portable unit in every model except functions, and even Lambda can run container images. Keep that, and moving is a configuration project, not a rewrite.

---

## 4.4 How to choose

Ask these, in order:

1. **What does the workload need that some models can't do?** Work that runs longer than minutes, persistent connections, GPUs, fixed outbound IPs, data that must stay in one country or one network. Each rules some models out.
2. **Who will operate it, and how much of their week can it take?** A solo developer or a team of three can't absorb Kubernetes; a company with a platform team may already run it, and joining that is cheaper than building something else.
3. **What do customers and compliance require?** Enterprise buyers ask about network isolation, encryption, separation of environments, and audit trails. All models can meet these, at different effort.
4. **What's the traffic shape?** Spiky and low favours functions and scale-to-zero; steady and high favours reserved capacity (containers, VMs, Kubernetes on discounted nodes).
5. **What does it cost at today's scale *and* at 10×?** Include people's time.

### By stage

| Stage | Typical good choice | Why |
|---|---|---|
| MVP | PaaS, or a single VM | Fastest to set up; cheapest; nothing is irreversible yet |
| Early production | PaaS (bigger plans) or managed containers | Zero-downtime deploys, managed database, health checks; still little to operate |
| Growth | Managed containers; Kubernetes if a platform team exists or services multiply | Autoscaling, private networking, many services; standardisation starts to pay |
| Enterprise | Whatever the organisation standardises on, often Kubernetes or managed containers across separate accounts | Consistency, policy enforcement, compliance evidence, shared tooling |

### By product type

- **B2B SaaS:** managed containers is the common sweet spot. Enterprise customers eventually ask for private connectivity, data residency or single-tenant deployments; models with real networking (containers, Kubernetes, VMs) make those possible.
- **AI / LLM products:** request handling is ordinary; the hard parts are **long-running inference or extraction**, **GPUs** if you host models, and **cost control**. Calling a hosted model API (as Ledgerline does) needs no GPU, but the work can take a minute: design for background jobs (Chapter 6) whatever the model.
- **Agentic workflows:** tasks run for minutes to hours, pause for tools or humans, and must survive restarts. That points to workers with durable state (Chapter 10), or workflow services (AWS Step Functions, Lambda durable functions, Temporal) rather than plain request handlers.
- **Internal tools:** a single VM or PaaS behind company single sign-on is often enough for years.
- **Consumer apps:** spiky, global traffic: CDN in front, autoscaling or serverless behind, and a hard look at cost per user.

### A sensible default

For a typical web product in 2026: **a PaaS until it hurts, then managed containers in your own cloud account**, with infrastructure as code from the day you move. Kubernetes when you have the people and the reasons, not before.

---

## 4.5 Kubernetes: when it's worth it

Kubernetes is the industry standard for running containers at scale, and the most common case of a team adopting a tool for the company they hope to become rather than the one they are.

**Worth it when:**
- You run many services (roughly ten or more) and several teams deploy independently.
- A platform team (even two people) can own the cluster, its upgrades and its add-ons.
- You need what its ecosystem gives: operators for databases and queues, service meshes, GPU scheduling, policy engines, the same platform on several clouds or on-premises.
- Customers require deployment into *their* environment, and Kubernetes is what they run.

**Not worth it when:**
- You have one to five services and a small team.
- Nobody on the team has run it in production.
- The main reason is "we'll need it eventually" or "it's on the job description".

**What running it involves**, beyond the cluster itself: an ingress controller and certificates, a way to get secrets from your secrets manager into pods, autoscaling for pods and for nodes, observability agents, network policies, upgrades of Kubernetes every few months and of every add-on, and on-call knowledge of all of it. Managed options (EKS Auto Mode, GKE Autopilot) take over node management, for a fee, and reduce, not remove, the work.

**The middle path:** learn the concepts locally (part 2 runs Ledgerline on a local cluster with *kind*), keep your app Kubernetes-ready (container image, health checks, config from the environment, stateless processes), and adopt it when the reasons above appear. Everything Ledgerline already does for ECS translates directly.

---

## 4.6 Serverless: fits and traps

Functions are excellent at what they're designed for, and awkward at what they're not.

**Fits:**
- Webhooks, scheduled jobs, file-arrival processing, queue consumers.
- APIs with low or very spiky traffic, where paying nothing when idle matters.
- Glue between managed services.

**Traps:**

| Trap | What happens | Mitigation |
|---|---|---|
| **Work after the response** | A function's execution freezes once it returns; background threads and "fire and forget" tasks don't reliably run | Hand off to a queue and a separate function or worker (Chapter 6); Ledgerline's background extraction hits exactly this in part 2 |
| **Time limits** | Lambda stops at 15 minutes | Break work into steps; use a workflow service (Step Functions, Lambda durable functions) |
| **Cold starts** | The first request to a new instance waits for start-up: tens of milliseconds to seconds, depending on runtime and package size | Smaller packages, faster runtimes, provisioned concurrency (paid) |
| **Database connections** | Hundreds of concurrent functions each open a connection; Postgres runs out | A connection pooler or proxy (RDS Proxy), or a database built for it (DynamoDB, Aurora Serverless with a data API) |
| **Local development** | Emulators differ from the real thing | Keep logic in plain code that runs anywhere; keep the handler thin |
| **Cost at sustained load** | Per-millisecond billing is cheap when idle and expensive when busy all the time | Compare with containers at your real load; Lambda Managed Instances (EC2 capacity at a 15% management fee) exist for steady workloads |

Prices, for scale (AWS, October 2026): **$0.20 per million requests** and **$0.0000166667 per GB-second** of compute, with **1 million requests and 400,000 GB-seconds free each month**. A small API with a few thousand requests a day costs nothing in compute; the database usually dominates.

---

## 4.7 Production-grade networking

Ledgerline's staging uses the account's default VPC with public subnets: cheap, and safe enough because every resource is locked down individually (Chapter 2). Production usually goes further.

### A VPC with public and private subnets

```
VPC 10.0.0.0/16, across 2–3 availability zones
├── public subnets   load balancer, NAT gateway(s)         ← reachable from the internet
├── private subnets  app containers                         ← no inbound from the internet
└── data subnets     database, cache                        ← reachable only from the app
```

The app and the database have **no public IP at all**. Even a misconfigured security group can't expose them to the internet directly.

### Outbound traffic from private subnets

Private resources still need to reach things outside: the container registry, S3, Parameter Store, CloudWatch, your LLM provider. Options:

| Option | Cost (AWS) | Notes |
|---|---|---|
| **NAT gateway** | ~$0.045/hour in US regions, ~$0.059/hour in Sydney (~$33–43/month) **per gateway**, plus ~$0.045 per GB processed | One per AZ for resilience; often the single biggest line in a small production bill. AWS added a **regional NAT gateway** in late 2025 that spans AZs and bills per active AZ |
| **Gateway endpoints** for S3 and DynamoDB | Free | Always add them: S3 traffic then bypasses NAT charges |
| **Interface endpoints** (ECR, Secrets Manager, CloudWatch, SSM…) | ~$0.01/hour per endpoint per AZ, plus data | Cheaper than NAT only when you need few services and little internet access; you can't reach external APIs (like an LLM provider) through them |
| **Public IPs on the tasks** (Ledgerline staging) | $0.005/hour per IP | Cheapest; tasks are in public subnets, protected by security groups only |

An LLM-calling app needs real internet egress, so production Ledgerline uses NAT plus free gateway endpoints for S3.

### Multiple availability zones

An availability zone is a separate data centre (or group of them) within a region. Production spreads app tasks over at least two, and runs the database **Multi-AZ** (a synchronous standby in another zone, with automatic failover in a minute or two). Cost: roughly double the database. Staging doesn't need it; production usually does, because "one data centre had a power problem" shouldn't be your outage.

### Domains, certificates and the edge

- **A domain you own** (`app.example.com`), with DNS at your registrar or Route 53.
- **Certificates** from AWS Certificate Manager (free for use with AWS load balancers and CloudFront, renewed automatically) or Let's Encrypt (Caddy on a VM does this automatically).
- **HTTPS all the way:** browser → CDN → load balancer → app, rather than HTTP inside (Chapter 2's staging has HTTP between CloudFront and the load balancer, because the load balancer has no certificate without a domain).
- **A web application firewall** (AWS WAF, Cloudflare): managed rule sets for common attacks, rate limits, bot control. A few dollars to tens of dollars a month at small scale (Chapter 8).

> Ledgerline doesn't use a custom domain in this guide. The setup is: register a domain → create an ACM certificate (validated with a DNS record) → attach it to the load balancer's HTTPS listener and to CloudFront as an alternate domain name → point a DNS record at CloudFront. About an hour of work and the domain's yearly fee.

### Egress: the cost nobody estimates

Data leaving AWS to the internet costs around $0.09 per GB in US regions after the free allowance, more in some regions; data crossing AZs costs a little each way; NAT adds its own per-GB charge. A product that serves large files or streams video should model this before choosing a provider.

---

## 4.8 Environments, accounts and promotion

### Separate accounts per environment

At the growth stage, production moves into **its own cloud account** (AWS Organizations, Google Cloud projects, Azure subscriptions). Reasons:

- **Blast radius:** a mistake or a compromised credential in staging can't touch production.
- **Permissions:** most engineers get broad access to development accounts and narrow, audited access to production.
- **Cost visibility:** each environment's bill is separate.
- **Compliance:** auditors look for exactly this separation.

A common layout: a management account (billing, nothing else), a shared-services account (CI, container registry), and one account each for development, staging and production. Ledgerline stays in one account for cost and simplicity; the new AWS sign-up experience makes extra accounts *projects*, but its restrictions (Chapter 2) make cross-account CI awkward.

### Same code, different values

Environments should differ in **values**, not in **code**: instance sizes, counts, Multi-AZ on or off, domain names, retention periods. In Terraform, that means **modules** for the shared shape and a small folder per environment that calls them:

```
infra/
├── modules/
│   ├── network/      VPC, subnets, NAT, endpoints
│   ├── database/     RDS with environment-specific sizing
│   └── service/      ECS service, load balancer, alarms
└── environments/
    ├── staging/      main.tf: module calls with staging values
    └── production/   main.tf: same modules, production values
```

If staging and production are two copies of the same files, they drift: someone fixes staging, forgets production, and the two stop being the same system.

### Promotion: build once, deploy many

Chapter 2's principle, extended across environments: **the image tested in staging is byte-for-byte the image that reaches production.** The pipeline:

```
push to main → test → build image (tagged with commit SHA) → deploy to staging → checks pass
     → approval (a human, for production) → deploy the SAME image to production → verify
```

- **Never rebuild for production.** A rebuild can pull different dependency versions or base-image updates; what you tested isn't what you ship.
- **An approval step** for production: GitHub *environments* can require named reviewers before a job runs, and restrict which branches may deploy.
- **Production migrations** run the same way as staging's (Chapter 2), which is why staging must run them first.
- **Each environment has its own credentials and configuration**; the image has none.

### Ephemeral environments

At the growth stage, teams create short-lived environments per pull request (preview environments): infrastructure as code plus a cheap model (PaaS previews, Cloud Run revisions, a namespace in a shared Kubernetes cluster) make it practical. They're what makes "test it like production" cheap enough to do for every change.

---

## 4.9 Moving between models

Ledgerline has run on four platforms without application changes because it follows a handful of rules, most from the [Twelve-Factor App](https://12factor.net):

| Rule | Ledgerline | Why it keeps you portable |
|---|---|---|
| One container image, any environment | Multi-stage Dockerfile, tagged by commit | Every model except plain functions runs images |
| Configuration from the environment | `app/config.py`; secrets injected | Each platform injects variables its own way; the app doesn't care |
| Stateless processes | Files in S3, state in Postgres | Instances can be replaced, scaled, moved |
| Logs to stdout | JSON lines | Every platform collects stdout |
| Health endpoints | `/livez`, `/readyz` | Every orchestrator needs them |
| Migrations as a separate step | `migrate` task, Compose service, Kubernetes Job | The same command, scheduled by each platform |
| Standard protocols for telemetry | OpenTelemetry, OTLP | Backend changes are configuration |
| Background work behind an interface | `extract_invoice(invoice_id)` is idempotent and takes an ID | Can move to a queue worker or a function (Chapter 6) |

**Where lock-in actually comes from** isn't usually compute; it's **data and managed services**: a proprietary database, a queue with no equivalent, identity tied to one provider, years of data in one region with egress fees to leave. Use managed services freely (they're why the cloud is worth paying for), but prefer ones with standard interfaces (Postgres, S3-compatible storage, OpenTelemetry, OIDC) when the choice is otherwise equal.

---

## 4.10 Challenges and watch-outs

- **Choosing for the future company.** Kubernetes for a three-person team; a multi-region setup with no customers. The cost is months of engineering time.
- **Choosing for today and never revisiting.** The PaaS bill that doubled every quarter; the single VM that became critical without backups.
- **The NAT gateway surprise.** A private-subnet design whose networking costs more than its compute.
- **Snowflake environments.** Staging and production built by hand, slightly differently, so staging stops predicting production.
- **Rebuilding for production.** Different bytes from the ones that were tested.
- **Background work on the wrong model.** Threads in a function that freezes after responding; long jobs in a request with a 30-second load balancer timeout.
- **Ignoring the operations budget.** Every model has patching, upgrades and incidents; the ones that hide them charge for it, the ones that expose them need someone's time.
- **Signals you've outgrown your model:** deploys need someone to SSH in; the platform's limits shape product decisions; infrastructure costs grow faster than usage; customers ask for things (private networking, residency) you can't offer.

---

## 4.11 Exercise

Choose a deployment model, and say what would make you change it:

1. A two-person startup's B2B MVP: a web app, a Postgres database, ~50 users.
2. An internal tool for a 300-person company, used during business hours, behind single sign-on.
3. A webhook receiver that gets 10 requests on most days and 50,000 on the first of each month.
4. A B2B SaaS with 40 enterprise customers, five services, eight engineers, and a customer asking for a dedicated deployment in their own AWS account.
5. An AI document-processing API where each job takes 2–20 minutes and traffic is steady through the day.

<details>
<summary>Suggested answers</summary>

1. **PaaS** (or one VM if budget is extremely tight). Revisit when the bill or a platform limit starts shaping decisions, or when enterprise customers ask for things the PaaS can't do.
2. **A single VM or PaaS** behind the identity provider, with automated backups. Revisit if it becomes business-critical enough to need zero-downtime deploys and multi-AZ.
3. **Serverless functions** with a queue in front if processing is slow. Paying for idle capacity 29 days a month makes no sense. Revisit if processing exceeds function limits.
4. **Managed containers in separate accounts per environment**, with infrastructure as code that can be applied to a customer's account (a "dedicated deployment" is the same modules with different values). Kubernetes becomes reasonable if services keep multiplying or customers require it; it's a platform-team decision.
5. **Managed containers with a queue and workers** (Chapter 6), not functions (time limits and steady load) and not request handlers (load balancer timeouts). Scale workers on queue depth.

</details>

---

## 4.12 Cost

The same small app (one web service, one worker or background process, a small Postgres database, file storage, light traffic), roughly, per month, October 2026 list prices. Treat these as orders of magnitude; regions and plans vary.

| Model | Example | Approx. monthly | Mostly |
|---|---|---|---|
| PaaS | Render: web service $7 + Postgres 1 GB $19 (+ $25 Pro workspace for a team) | ~$26–51 | The managed database |
| PaaS | Railway: usage-metered, Hobby $5 including $5 of usage | ~$15–30 | Metered CPU and memory |
| Single VM | A 2 GB VM with Postgres on it, plus backups | ~$10–25 | The VM; backups are your job |
| Managed containers | Ledgerline staging on ECS (Chapter 2): Fargate task, load balancer, public IPs, RDS | ~$65 | Load balancer, IPs and database, not compute |
| Managed containers, production-grade | Two tasks in two AZs, NAT, Multi-AZ RDS, WAF | ~$150–250 | NAT gateways and Multi-AZ database |
| Kubernetes (EKS) | Control plane $73 + two small nodes + load balancer + RDS | ~$170–250 | Control plane and nodes before any app traffic |
| Serverless | Lambda + function URL + RDS, low traffic | ~$20–30 | The database (compute is in the free tier) |
| Scale-to-zero containers | Cloud Run with request-based billing + a small managed database | ~$10–40 | The database; Cloud Run's free tier covers light traffic |

What the table hides:
- **People's time**, which dwarfs every number above for all but the largest systems.
- **The step changes**: a NAT gateway, Multi-AZ, a second environment, a WAF each add a fixed amount regardless of traffic.
- **Discounts at steady load**: reserved capacity and savings plans cut compute costs by a third or more once usage is predictable.

---

## 4.13 Demo

[Part 2, Deployment models in practice](04b-deployment-demos.md), runs the same Ledgerline image four more ways:

1. **Kubernetes, locally with kind:** namespaces, Deployments, a migration Job, probes, Secrets. Free.
2. **A single VM:** EC2 with Docker Compose and Caddy, created and destroyed with Terraform.
3. **Serverless:** FastAPI on Lambda, and what breaks (background extraction).
4. **Production on AWS:** Terraform modules, a production environment with private subnets and a Multi-AZ database, and a CI pipeline that promotes the staging-tested image to production after approval.

---

## 4.14 Checklist

Your deployment model is a deliberate choice when:

- [ ] You can say why this model fits your stage, team and workload, and what would make you change it
- [ ] The application is portable: one image, configuration from the environment, stateless processes, health endpoints, migrations as a separate step
- [ ] Background and long-running work runs somewhere suited to it, not in request handlers or frozen functions
- [ ] Production's app and database have no public IP, or you've accepted and documented why they do
- [ ] Production spans at least two availability zones, or you've accepted the risk in writing
- [ ] Outbound traffic is designed: NAT, endpoints or public IPs, with the cost known
- [ ] Production has its own domain and certificates, HTTPS end to end
- [ ] Staging and production are built from the same infrastructure code with different values
- [ ] The image deployed to production is the one tested in staging, and production deploys need an approval
- [ ] Production is separated from other environments by account, or at least by strict permissions
- [ ] You know the monthly cost floor of your setup, and what the next step change will cost

---

**Next:** [Chapter 5, The data layer](05-data-layer.md): Postgres in production, backups and restores at scale, read replicas, caching, search and the data that lives outside the database.
