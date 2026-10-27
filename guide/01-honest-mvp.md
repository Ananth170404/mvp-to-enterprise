# Chapter 1: The honest MVP

> **In this chapter:** what an MVP is actually for, how AI products change the definition, which shortcuts are safe and which quietly become one-way doors, and how to scope and build a first version that tests something real.

---

## 1.1 Why it matters

Most MVPs fail in one of two ways:

- **Too big.** A small version of the final product: login, settings, billing, dashboards, a bit of everything. Months pass before anyone learns whether the core idea works.
- **Too careless.** Built so fast that the shortcuts become permanent. The "throwaway" code goes to production, and the team spends the next year migrating data that was stored the wrong way.

An honest MVP avoids both: it is **small in features and strict about data**.

---

## 1.2 What an MVP is for

> **An MVP is the smallest thing that tests your riskiest assumption.**

It is not a small version of the final product. "Small version of the product" pulls you toward building a little of everything. "Test the riskiest assumption" pulls you toward building only the part that could prove you wrong.

The riskiest assumption depends on what you are building:

| Product type | Usual riskiest assumption |
|---|---|
| B2B SaaS | Will companies pay for this, and trust it with their data? |
| AI / LLM product | Can the model do the task **reliably enough**, at a **cost per task** that works? |
| Agentic system | Can it take actions safely enough that people let it act unsupervised? |
| Internal tool | Will the team actually change how they work to use it? |
| Consumer app | Will people come back after the first week? |

A useful honesty check before you build:

> **If this MVP succeeds, what will we know that we don't know today?**

If the answer is "that we can build software", it isn't testing anything.

---

## 1.3 AI products: the eval set is part of the MVP

For a traditional app, tests check that the code does what you wrote. For an AI product, you also need to know whether **the model does what you need**, and that can't be unit-tested in the usual way. Output varies, models change underneath you, and a prompt tweak that fixes one case can break three others.

The answer is an **evaluation set**: a collection of real inputs with the correct expected outputs, scored automatically.

- Start with **20–50 examples**. A spreadsheet is fine at first.
- Include the hard cases on purpose: messy inputs, edge cases, the things you expect the model to get wrong.
- Score each output field by field, and record **cost and latency** alongside accuracy.
- Re-run it whenever you change the prompt, the model or the provider.

Without evals, every change is a guess. With them, "is the model good enough?" becomes a number you can track, and "should we switch to the cheaper model?" becomes a decision you can make with data.

Two things to watch when reading results:

- **A failed call is not a wrong answer.** Rate limits, timeouts, retired models and rejected requests show up as missing outputs. Report them separately from accuracy, grouped by cause, or a model that never answered looks the same as a model that answered badly.
- **A perfect score means the eval is too easy,** not that the model is perfect. Once the model passes everything, the eval can no longer tell you whether a change made things worse. Add harder and more realistic examples until it fails sometimes.

**Where examples come from:**

1. **Synthetic data you generate yourself.** You know the correct answer for every example by construction, and you can inject hard cases deliberately. Ideal for getting started and for safe public demos.
2. **Public datasets.** Useful for realism and variety. Check licences, and be careful with machine-generated labels, which can contain the same mistakes your model is meant to avoid.
3. **Your users' corrections.** Every time a human fixes a model output, you have a new, real, labelled example. Design the product to capture these from day one. Over time, this becomes your best eval set by far.

> **Do not send real customer data to a provider whose terms allow it to train on your inputs.** Many free tiers do. Use free tiers with synthetic data only.

---

## 1.4 Shortcuts: safe versus dangerous

The uncomfortable truth: **MVP code almost never gets thrown away.** It becomes production code. So the question isn't whether to take shortcuts (you must), but which ones.

### Safe shortcuts

These are two-way doors with a small blast radius. Take them freely.

- One service, one database; a monolith
- A single server or a PaaS; simple, even semi-manual deploys
- No caching, no queue, no separate workers; background work in-process
- Server-rendered pages instead of a separate frontend app
- Admin tasks through scripts or a database console
- Manual customer onboarding
- Tests only on the critical paths

### Dangerous shortcuts

These turn into one-way doors, or carry a large blast radius. Avoid them even at day one.

| Shortcut | Why it's dangerous | The cheap fix |
|---|---|---|
| No tenant separation | Mixing customers' data is catastrophic and very hard to untangle later | `tenant_id` on every table, filtered in every query |
| Secrets in code or git | They leak, and git history keeps them forever | Environment variables; `.env` in `.gitignore` from the first commit |
| No backups | One bad command ends the company | Managed database with automated backups, restore tested once |
| Hand-edited schema | Environments drift; no one knows what production looks like | A migrations tool from the first table |
| Home-made password handling | Breaches | An auth provider or vetted library |
| Money stored as floats | `0.1 + 0.2 ≠ 0.3`; totals drift by paise or cents | Integer minor units plus a currency code |
| Mutable history | Financial, approval and compliance records must show who changed what | Append-only audit log |
| Sensitive data in logs | Logs get copied everywhere | Log IDs, not contents |
| Code coupled to one LLM SDK | Models get deprecated and repriced; enterprise buyers mandate providers | A thin internal wrapper; model ID in config |
| Trusting the model's arithmetic or validation | Models are good at reading, unreliable at checking | Deterministic code for anything that can be computed |

The rule of thumb:

> **Be lazy about features. Be strict about data.**

Features are easy to add later. Corrupted, mixed or lost data usually can't be fixed.

### Do you need a queue (Celery, Redis, …) yet?

Usually not. A background job framework solves real problems: many workers, retries that survive restarts, scheduled jobs, bursts of traffic. At MVP scale you rarely have them, and you pay for the extra moving parts every day.

The cheap version:

1. Model slow work as **state**: a status field such as `uploaded → processing → done | failed`, so nothing assumes the work is instant.
2. Put the work in **one function that takes an ID**, opens its own database session and is **safe to run twice**.
3. Run it in-process (your framework's background tasks), or use the database itself as a queue (`SELECT … FOR UPDATE SKIP LOCKED`) if you need a little more.

When the signals appear (requests timing out, work lost on restart, a need for scheduled jobs or many workers), moving to a real queue only changes **how the function is called**. That move is covered in Chapter 6.

---

## 1.5 How it differs by product type

**B2B SaaS.** Recruit two to five *design partners*: real companies that use the product early in exchange for influence over it. Manual onboarding is expected. Tenant separation and an audit trail matter from day one, because the first security questionnaire may arrive sooner than you think.

**AI / LLM product.** Keep a human reviewing every output at first. It's safer, it builds trust, and every correction becomes an eval example. Track cost per task from the first call; it's part of the riskiest assumption, not a later optimisation.

**Agentic system.** Start with the agent *proposing* actions and a human *approving* them. Log every proposed and executed action. Autonomy is something the product earns with data, not a launch feature.

**Internal tool.** Pick one team. Use the company's existing login rather than building accounts. You can skip billing and onboarding entirely, but not access control: internal tools often touch the most sensitive data a company has.

**Consumer app.** The bar is higher than founders expect. Onboarding and the first five minutes *are* the product, because retention is what you are testing. If there's a free tier and an AI feature, cost per user can sink you before growth does; cap it from day one.

---

## 1.6 Challenges and watch-outs

- **Scope creep disguised as polish.** "Just add a settings page" is how MVPs triple in size. Ask whether each feature tests the riskiest assumption.
- **Demo-driven accuracy.** An AI feature that works on the five examples you tried is not reliable. Only the eval set tells you.
- **Model drift and deprecation.** Providers retire, restrict, reprice and silently redirect models. Your product changes even when your code doesn't. (See the worked example below.)
- **Trusting second-hand sources about providers.** Blog posts and aggregators lag or contradict official docs. For model availability and pricing, check the provider's own deprecation and pricing pages.
- **Free-tier traps.** Low rate limits, terms that allow training on your data, and limits that can change without much notice.
- **"We'll add tenant_id later."** You won't, or it will cost a migration of every table and every query.

**Signals you've outgrown the MVP:**

- Real users depend on it daily, or money moves based on its output.
- You are fixing the same production problem by hand more than twice.
- A customer asks a security or compliance question you can't answer.
- Background work is getting lost, timing out or piling up.

These signals mean you are entering Stage 1, which is what Chapters 2 and 3 cover.

---

## 1.7 How to build it

A sequence that works for most products:

1. **Write down the riskiest assumption** and what result would count as success or failure.
2. **Design the data model carefully**, since it is the most expensive thing to change. Get tenancy, IDs, money and history right.
3. **Set up the boring foundations once:** a container, environment-based config, a migrations tool, `.gitignore` for secrets.
4. **Build the single core flow end to end**, with the simplest possible UI.
5. **For AI products, build the eval harness alongside the feature,** not after it.
6. **Put deterministic checks around the model.** Let the model read and interpret; let code validate and calculate.
7. **Capture human corrections** as structured data.
8. **Ship to design partners**, measure, and decide what's next based on the riskiest assumption.

---

> ### Worked example: Ledgerline v0
>
> **Riskiest assumption:** an LLM can extract invoice data accurately enough, and the system can catch duplicates and mismatches reliably enough, that a finance person trusts it more than manual entry.
>
> **In scope:** upload a PDF or image → the model extracts vendor, GSTIN, invoice number, date, PO number, line items and totals → code checks the result → a person reviews and corrects it → an approver approves or rejects it → everything is in an append-only audit log. Purchase orders are imported from CSV.
>
> **Out of scope:** email ingestion, ERP integrations, payments, agents taking actions, real authentication, a queue, a polished UI.
>
> **How the dangerous shortcuts were avoided:**
>
> | Concern | What v0 does |
> |---|---|
> | Tenancy | `tenant_id` on every table; every lookup filters by it; another tenant's invoice returns 404 |
> | Money | Integer paise plus currency code; the model returns amounts as *strings*, parsed with `Decimal` |
> | Duplicates | `sha256` per file, unique per tenant, so the same upload is idempotent; code also flags a repeated invoice number from the same vendor |
> | History | `audit_events` is insert-only in app code, and a database trigger rejects any `UPDATE` or `DELETE` |
> | Workflow | An explicit state machine: `uploaded → extracting → needs_review → approved / rejected` |
> | Model coupling | One wrapper interface; model ID in config; a `fake` provider so the app runs with no key |
> | Cost | Every model call stored with model, prompt version, tokens, cost and latency |
> | Checks | Plain code verifies arithmetic, the GSTIN checksum, PO match and duplicates; errors need a written reason to override |
> | Auth | A clearly labelled dev-only login that the app refuses to enable in production; real auth in Chapter 7 |
> | Background work | Extraction is one idempotent function taking an invoice ID, run as an in-process background task |
>
> **The model deprecation story.** While building v0, the plan was to use Gemini 2.5 Flash. Checking showed a messy situation. Google's deprecation page (as of October 2026) says 2.5 models are not deprecated but are being limited to users who have used them before, with new projects pointed to newer models. Meanwhile third-party articles reported a fixed shutdown date, and developers on Google's forum reported their production apps receiving "model no longer available" errors months earlier than expected. The same page also shows an older model ID that now **silently routes requests to a newer model**, meaning behaviour can change with no code change at all.
>
> The lessons are general: keep model IDs in config, check the provider's own pages rather than summaries, and keep an eval set so a silent change shows up as a number.
>
> **Data.** A generator creates synthetic GST invoices with known correct answers, across three layouts and six scenarios (clean, PO mismatch, no PO, arithmetic error, duplicate re-send, and a rotated "phone scan" image). It also writes the matching purchase orders. Generation is deterministic, so the same seed produces byte-identical files.

---

## 1.8 Cost

At MVP stage, the main costs are your time and, for AI products, model calls. Infrastructure can be close to zero: a laptop, a free PaaS tier or new-account cloud credits (Chapter 4).

For model calls, estimate cost per task, then measure it.

> **Worked example: Ledgerline extraction cost.** Prices from Google's Gemini pricing page, paid Standard tier, checked October 2026. The estimates below assumed about 1,500 input and 500 output tokens per one-page invoice, before anything was measured.
>
> | Model | Input / output per 1M tokens | Est. per invoice | 1,000 invoices / month |
> |---|---|---|---|
> | gemini-3.5-flash-lite | $0.30 / $2.50 | ~$0.0017 | ~$1.70 |
> | gemini-3.1-flash-lite | $0.25 / $1.50 | ~$0.0011 | ~$1.10 |
> | gemini-3.8-flash (until Dec 2026) | $0.75 / $3.75 | ~$0.0030 | ~$3.00 |
> | gemini-3.8-flash (from Jan 2027) | $1.50 / $7.50 | ~$0.0060 | ~$6.00 |
>
> **Then we measured.** The first real eval run, on all 30 synthetic invoices:
>
> | Model | Measured cost per invoice | All fields correct | Notes |
> |---|---|---|---|
> | gemini-3.5-flash-lite | ~$0.0011 | 30 / 30 | Including the three rotated "phone scan" images |
> | gemini-3.8-flash | — | not measurable | Most calls failed before answering, so there was nothing to score |
>
> Three lessons:
>
> 1. **The estimate was about 50% too high.** Real token counts were lower than assumed. Estimates are for deciding whether to start; measurements are for decisions.
> 2. **30 out of 30 means the eval set is now too easy.** It no longer distinguishes a good change from a bad one. The next step is harder cases: multi-page invoices, handwriting, missing fields, real-world layouts (Chapter 9).
> 3. **The second model's failures were calls that never answered, not wrong answers.** The eval script now groups failures by cause (rate limit, timeout, retired model, rejected request), because each one has a different fix.
>
> Two broader points: extraction is cheap enough that cost isn't the riskiest part; accuracy is. And one model's price is scheduled to double, which is why the pricing table in the code carries effective dates instead of a single number.

General guidance:

- Record cost per call from day one; you can't optimise what you didn't measure.
- "Thinking" or reasoning tokens are usually billed as output, even though users never see them.
- Set a hard spending cap on every paid API account.
- Estimate monthly cost at 10× and 100× your current volume; that's when pricing decisions and model choices start to matter (Chapter 12).

---

## 1.9 Demo

The reference implementation lives in [`ledgerline/`](../ledgerline/README.md). It runs with Docker and needs no API key by default.

```bash
cd ledgerline
cp .env.example .env
docker compose run --rm api python -m scripts.generate_invoices
docker compose up --build
# open http://localhost:8000
```

Then:

1. Upload a clean invoice and a `po_mismatch` invoice, and compare the checks.
2. Upload the same file twice.
3. Upload a `duplicate` invoice after its original.
4. As the approver, try to approve an invoice with errors.
5. Sign in as the other tenant's user and try to find Acme's invoices.
6. With a Gemini key, run the eval script and compare two models.

---

## 1.10 Checklist

Your MVP is honest when:

- [ ] You can state the riskiest assumption and what result would disprove it
- [ ] Every feature in scope helps test that assumption
- [ ] Every business table carries a tenant identifier (if you have more than one customer, ever)
- [ ] Money is stored as integers plus a currency, never floats
- [ ] Secrets are in environment variables and never in git
- [ ] Schema changes go through a migrations tool
- [ ] Records that need history are append-only
- [ ] For AI features: an eval set exists, with hard cases, scored automatically
- [ ] For AI features: model ID is in config, and each call's cost is recorded
- [ ] Anything the model could get wrong that code can verify is verified by code
- [ ] Human corrections are captured as data
- [ ] Slow work is modelled as state and runs in an idempotent function
- [ ] Dev-only shortcuts can't accidentally be enabled in production

---

**Next:** [Chapter 2 — Production readiness](02-production-readiness.md): environments, configuration and secrets, CI/CD, migrations in a pipeline, and backups you have actually restored.
