# From MVP to Enterprise

A practical, developer-focused guide to taking software from a working MVP to a production system that enterprises can trust — and knowing **when** to invest in each step, not just how.

The guide is product-agnostic. It applies to B2B SaaS, AI/LLM products, agentic systems, internal tools and consumer apps. Throughout, **Ledgerline** — an AI accounts-payable agent — is used as a worked example and reference implementation. Ledgerline sections are clearly marked and can be skipped.

## Who this is for

Developers who can build an MVP and want to understand what comes next: reliability, deployment, data, security, AI in production, cost, pricing and enterprise readiness.

## How each chapter is structured

1. **Why it matters** — what breaks if you ignore it
2. **By stage** — MVP → early production → growth → enterprise
3. **By product type** — B2B SaaS, AI/agentic, internal tool, consumer
4. **Challenges and watch-outs** — failure modes and signals you've outgrown your setup
5. **How to build it** — recommended approach, alternatives, trade-offs
6. **Cost** — rough numbers per stage
7. **Demo** — a small, runnable piece of Ledgerline
8. **Checklist** — "you're ready when…"

## Contents

| # | Chapter | Status |
|---|---|---|
| 0 | [Foundations](guide/00-foundations.md) | Draft |
| 1 | [The honest MVP](guide/01-honest-mvp.md) | Draft |
| 2 | [Production readiness](guide/02-production-readiness.md) — and part 2: [Staging on AWS with Terraform](guide/02b-staging-on-aws.md) | Draft |
| 3 | [Observability](guide/03-observability.md) — and part 2: [Observability on AWS](guide/03b-observability-on-aws.md) | Draft |
| 4 | Deployment models and infrastructure | Planned |
| 5 | The data layer | Planned |
| 6 | Architecture evolution | Planned |
| 7 | Auth, identity and multi-tenancy | Planned |
| 8 | Security | Planned |
| 9 | LLMs in production | Planned |
| 10 | Agentic workflows in production | Planned |
| 11 | Reliability and scale | Planned |
| 12 | Cost and pricing | Planned |
| 13 | Enterprise readiness | Planned |
| 14 | Team and process | Planned |
| 15 | Playbooks by product type | Planned |

## Repository layout

```
guide/        The guide, one Markdown file per chapter
ledgerline/   Reference implementation (see ledgerline/README.md)
```

## Running the demos

Demos run locally with Docker; see [ledgerline/README.md](ledgerline/README.md). Cloud demos (from Chapter 4) use AWS and are designed to fit within new-account free-tier credits.
