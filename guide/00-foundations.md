# Chapter 0: Foundations

> **In this chapter:** what "production" really means, the four stages a product moves through, and a decision framework for knowing *when* to invest in something — the skill every later chapter depends on.

---

## 0.1 What "production" actually means

An MVP answers one question: **does this work, and does anyone want it?**

Production answers a different one: **what happens when it doesn't work?**

That shift is the whole journey from MVP to enterprise. Once someone relies on your software — customers, employees, money, other systems — every failure has a cost: lost data, lost trust, lost revenue, sometimes legal exposure. Monitoring, backups, deployment pipelines, security, compliance: everything in the rest of this guide exists to manage that cost.

A useful working definition:

> **Your software is in production when someone other than you would be harmed if it stopped working or behaved incorrectly.**

Note that this has nothing to do with user count. An internal tool with twelve users that moves payroll data is in production. A consumer app with ten thousand users who treat it as a toy may barely be.

---

## 0.2 The four stages

Stages are best identified by **what is most likely to kill the product right now**, not by user counts. The numbers below are rough orientation only.

| Stage | You are here when… | Rough size | The biggest threat |
|---|---|---|---|
| **0. MVP** | You are still testing whether anyone cares | 0–100 users, 1–2 devs | Building something nobody wants |
| **1. Early production** | Real people depend on it; first paying customers | 100–10k users, 2–8 devs | Losing data or trust: outages, bugs, breaches |
| **2. Growth** | The system or the team has become the bottleneck | 10k–1M+ users, 8–50 devs | Slowing down: performance, cost blowups, engineers blocking each other |
| **3. Enterprise** | Buyers are organizations with procurement and security reviews | Fewer, larger customers; 50+ devs | Failing to win or keep large contracts because you can't prove trust |

Each stage changes what "good engineering" means:

- **Stage 0** rewards speed and learning. Code quality matters less than learning fast.
- **Stage 1** rewards not breaking things. Backups, monitoring and safe deployments start paying for themselves.
- **Stage 2** rewards throughput — of the system and of the team. Architecture, performance and process become central.
- **Stage 3** rewards *provable* trust. It is no longer enough to be secure; you must demonstrate it with SSO, audit logs, certifications, contracts and SLAs.

### Stages are not strictly sequential

- A **B2B product** can face enterprise demands — SSO, security questionnaires, data processing agreements — with ten customers, if those customers are large companies.
- A **consumer app** can reach millions of users and never need enterprise features at all, while hitting growth-stage scale problems early.
- An **internal tool** skips billing, onboarding and marketing entirely, but often hits security and compliance requirements on day one because it touches company data.
- An **AI or LLM product** hits cost problems earlier than a traditional app, because every request costs real money in tokens, and hits safety and correctness problems earlier because model output is non-deterministic.

Treat the stages as a set of concerns that switch on, not a ladder you climb rung by rung.

---

## 0.3 The decision framework

The most valuable skill in this guide is not knowing *how* to set up Kubernetes or SSO. It is knowing **when**.

- **Over-engineer early** and you spend months on infrastructure for users who never arrive.
- **Under-engineer** and you end up rebuilding critical systems under pressure, with customers watching.

Three questions decide almost every "should we build this now?" debate.

### Question 1: Is it reversible?

Borrowing Amazon's framing of one-way and two-way doors:

- **Two-way doors** are cheap to change later. Examples: hosting provider, CI system, frontend framework, monitoring vendor, most internal code structure.
- **One-way doors** become expensive to change once you have data, customers or integrations. Examples: core data model, how customer data is separated (tenancy model), ID formats, public API contracts, where data is legally stored, primary database.

Many decisions *start* as two-way doors and *become* one-way as data and dependencies accumulate. The database is the classic case.

### Question 2: What is the blast radius if it goes wrong?

Some failures are inconvenient: a slow page, a broken button. Others are catastrophic: processing a payment twice, showing one customer's data to another, losing records you are legally required to keep, leaking credentials.

**High-blast-radius concerns get addressed early, even at tiny scale.** "We only have ten users" is not a reason to risk mixing their data.

### Question 3: Is the pain here, or within one stage?

Don't build for problems three stages away. Do build for problems you can see arriving in the *next* stage — by the time they arrive you will be too busy to fix them calmly.

Useful leading indicators that a problem is approaching:

- The same incident or manual fix has happened more than twice.
- A sales conversation asked for it (enterprise features are usually requested before they are required).
- A metric is trending toward a known limit (database CPU, queue depth, monthly cloud or token spend).
- Onboarding a new engineer takes noticeably longer than it used to.

### Putting it together

|  | **Reversible** | **Irreversible** |
|---|---|---|
| **Low blast radius** | **Defer** until it hurts | **Decide carefully now**, build later |
| **High blast radius** | **Cheap version now** | **Do it properly now** |

### The most useful idea in this guide: the cheap version

Most production concerns have a version that costs roughly 10% of the effort and buys most of the safety. Doing the cheap version now keeps the expensive version possible later.

| Concern | Cheap version now | Full version later |
|---|---|---|
| Multi-tenancy | A `tenant_id` on every table, enforced in queries | Row-level security, per-tenant databases, dedicated deployments |
| Observability | Structured JSON logs and an error tracker | Metrics, distributed tracing, SLOs, alerting |
| Backups | Automated nightly database backups, tested once | Point-in-time recovery, cross-region replicas, regular restore drills |
| Deployment | Containerize with Docker | Orchestration, blue-green or canary releases |
| Enterprise login | Use an auth provider or library that supports SAML/OIDC | Full SSO and SCIM user provisioning |
| LLM provider | A thin internal wrapper; log model, tokens, cost and latency per call | Model routing, fallbacks, gateways, self-hosted models |
| Abuse and cost | Basic per-user and per-IP rate limits; spending caps on paid APIs | Per-plan quotas, burst allowances, usage-based billing |

Two patterns worth noticing:

1. **The cheap version of one decision often lives inside a different decision.** Docker keeps Kubernetes possible. The right auth provider keeps SSO possible. An LLM wrapper keeps model choice open.
2. **Context changes the answer.** Rate limiting is optional for an internal tool behind a VPN and urgent for a public AI product where each request costs money.

### Buy versus build

A fourth question sits alongside the three: **is this part of what makes your product different?**

If not — authentication, payments, email delivery, error tracking, observability tooling — default to buying or using a managed service. Engineering time at Stage 0 and 1 is the scarcest resource you have. Revisit the decision when cost, control or compliance requirements change, which typically happens at Stage 2 or 3.

---

> ### Worked example: Ledgerline
>
> **Ledgerline** is an AI accounts-payable agent. It reads vendor invoices, extracts line items, matches them to purchase orders, checks tax details, flags duplicates and anomalies, and routes invoices for approval. Later it acts on its own: chasing vendors for missing details, reconciling against bank statements and preparing payment batches for human sign-off.
>
> It can be shipped as a **B2B SaaS** for finance teams, an **internal tool** run by one company's finance department, a **consumer** receipt and expense tracker, and an **agentic** system that takes actions.
>
> Applying the framework to its MVP:
>
> - **Do properly now:** separate every company's data with a `tenant_id`; make anything payment-related idempotent so nothing is ever processed twice; keep an append-only audit log of who approved what. Money and financial records give these a very high blast radius.
> - **Decide carefully now:** the core data model — invoices, line items, vendors, purchase orders. Changing it later means migrating financial records.
> - **Cheap version now:** an LLM wrapper with cost logging; basic rate limits; Docker; an auth provider that can add SSO later.
> - **Defer:** Kubernetes, microservices, SSO itself, multi-region deployment. None of these hurt yet.

---

## 0.4 Exercise: classify these decisions

Try this before reading the answers. For a generic early-stage B2B SaaS, classify each as **do properly now**, **cheap version now**, **decide carefully now** or **defer**.

1. Choosing Postgres or MongoDB as your main database
2. Adding SSO (login with a company's Okta or Google Workspace)
3. Password storage and authentication
4. Setting up Kubernetes
5. Which LLM provider you call
6. Rate limiting your API

<details>
<summary><strong>Answers and discussion</strong></summary>

1. **Database — decide carefully now.** It feels reversible on day one but becomes a one-way door as data accumulates. For most B2B SaaS, Postgres is a strong default: relational for structured business data, JSONB for flexible documents, and pgvector for embeddings — one database covering three needs early on.

2. **SSO — defer building it; cheap version now in your auth choice.** The cheap version isn't partial SSO; it is picking an auth provider or library that supports SAML/OIDC, so SSO becomes configuration rather than a project when the first enterprise customer asks.

3. **Authentication — do properly now.** Huge blast radius, cheap fix: don't build your own. Use a provider, or a vetted library with a modern password hash such as argon2 or bcrypt.

4. **Kubernetes — defer.** The cheap version is containerizing with Docker. Exception: if you sell a self-hosted or on-prem version, a Helm chart may become a product deliverable early.

5. **LLM provider — cheap version now.** Provider-specific details (tool-calling formats, prompt tuning, response parsing) leak into code quickly. Wrap calls behind your own interface and log model, tokens, cost and latency from day one. Enterprise buyers often require models served from their own cloud account, which a wrapper makes far easier.

6. **Rate limiting — cheap version now for public products, defer for internal tools.** For AI products especially, one abusive user, scraper, or your own retry bug can burn a month's budget overnight, and login endpoints need protection against password guessing. Add basic limits and provider spending caps now; build per-plan quotas when pricing tiers exist.

**What to take away:** labels depend on context; cheap versions often live in adjacent decisions; and AI products pull several concerns earlier because cost is incurred per request.

</details>

---

## 0.5 Checklist

You have the foundations in place when you can:

- [ ] Say which stage your product is in, based on its biggest current threat rather than its user count
- [ ] Name which stage concerns your product type switches on early (enterprise demands, compliance, per-request cost)
- [ ] List your product's one-way doors and confirm each was a deliberate decision
- [ ] Identify your highest-blast-radius failure modes and what protects against each
- [ ] For each concern you are deferring, name the cheap version you are doing instead — or state explicitly that none is needed
- [ ] Identify which parts of your stack are differentiating, and which you should buy rather than build

---

**Next:** [Chapter 1 — The honest MVP](01-honest-mvp.md): what a real MVP looks like, which shortcuts are fine, and which ones quietly become one-way doors.
