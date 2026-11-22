# Chapter 8: Security

> **In this chapter:** making the system hard to misuse and quick to recover when someone tries. Threat modelling, the OWASP Top 10 as a working checklist, browser defences (security headers, content security policy, CSRF), trusting the network only as far as you should, abuse limits, secrets, the software supply chain (dependencies, images, CI actions, SBOMs, signing), infrastructure scanning and accepted risk, encryption, audit logs and detection, incident response, and security testing.
>
> **Part 2, [Security in practice](08b-security-demos.md),** hardens Ledgerline's web app against a ZAP scan (from 9 warnings to 3) and finds a real flaw on the way: a forged header could choose the client's IP address. Then CI gains an infrastructure scan with expiring exceptions, an image vulnerability scan, an SBOM, and keyless signing that each deploy verifies.

---

## 8.1 Why it matters

The earlier chapters built something that works. This one is about how it fails when someone *wants* it to fail. That person doesn't follow your happy path; they read your HTML, replay your requests, change headers, and look at everything your build pulls in.

Security work has an odd shape:

- **Most attacks are boring.** Stolen credentials, a missing permission check, an outdated library with a public exploit, a misconfigured bucket. Not zero-days.
- **The supply chain is now part of your attack surface.** Your code is a small fraction of what you ship. In March 2026 attackers rewrote nearly every version tag of a popular security scanner's GitHub Action to steal CI secrets (8.9); thousands of pipelines ran it.
- **Customers check before they buy.** Security questionnaires, penetration test reports and SOC 2 (Chapter 13) all ask the questions in this chapter.
- **It's cheaper early.** A CSP is one header on a new app and a refactor on an old one. Signing images costs nothing to start and is painful to retrofit across many services.

> **Security is a property of the whole system, kept up continuously.** No single tool provides it. Each control here removes a class of mistakes, and the checks in CI keep them removed.

---

## 8.2 The words

| Term | Means |
|---|---|
| **Threat model** | A structured answer to "what could go wrong, and what do we do about it?" for a system |
| **Attack surface** | Everything an attacker can reach: URLs, forms, APIs, uploads, queues, CI, dependencies, people |
| **CSP** (Content Security Policy) | A response header telling the browser which scripts, styles, frames and form targets a page may use |
| **CSRF** (cross-site request forgery) | Another site making a signed-in user's browser submit a request to yours |
| **XSS** (cross-site scripting) | Getting your page to run an attacker's script |
| **SSRF** (server-side request forgery) | Getting your server to fetch a URL the attacker chooses, often an internal one |
| **CVE** | A published, numbered vulnerability in a specific product and version |
| **SBOM** (software bill of materials) | A machine-readable list of every component in an artifact, with versions |
| **Signing / attestation** | Cryptographic proof of who built an artifact; an attestation also carries a signed statement about it (its SBOM, how it was built) |
| **SAST / DAST / SCA** | Static analysis of code; dynamic testing of a running app; software composition analysis (known-vulnerable dependencies) |

---

## 8.3 Threat modelling: four questions

You don't need a methodology to start. Answer four questions, on one page, per feature or per system:

1. **What are we working on?** A sketch: the user, the browser, CloudFront, the app, the database, S3, the queue, the worker, the model provider, the IdP, CI.
2. **What can go wrong?** Walk each arrow and each box. The **STRIDE** prompts help: **S**poofing (pretending to be someone), **T**ampering (changing data or code), **R**epudiation (denying having done something), **I**nformation disclosure, **D**enial of service, **E**levation of privilege.
3. **What are we going to do about it?** Mitigate, accept (with a reason and a date), transfer (insurance, a provider), or remove the feature.
4. **Did we do a good job?** Tests and scans that prove the mitigations still hold.

> **Ledgerline's** one-page model has these top entries:
>
> | Threat | Where | Mitigation |
> |---|---|---|
> | Another tenant's invoices (information disclosure) | Every query | Tenant filters plus row-level security (Chapter 7) |
> | A malicious PDF uploaded (tampering, elevation) | Upload, extraction | Type and size checks; PDFs never rendered by the server; the model's output is treated as data, never as instructions (Chapter 9) |
> | Script injected into a page (XSS) | Templates | Auto-escaping; a CSP with no scripts allowed (8.5) |
> | A cross-site form submits an approval (CSRF) | Every POST | SameSite cookies, origin checks, CSRF tokens (8.5) |
> | Password guessing / sign-in floods (DoS, spoofing) | Sign-in | IdP lockouts plus per-IP limits in the app (8.7) |
> | A poisoned dependency or CI action (tampering) | Build | Pinned versions and SHAs, scanning, signed images verified at deploy (8.9) |
> | Leaked AWS keys (elevation) | CI secrets | Push-only and deploy-only users, one per environment; no long-lived keys on laptops (Chapter 4) |

Update it when the architecture changes: a new integration, a new data type, a new way in.

---

## 8.4 The OWASP Top 10 as a working checklist

OWASP's list of the most critical web application risks was revised for 2025, the eighth edition. Two categories are new, Software Supply Chain Failures and Mishandling of Exceptional Conditions, and SSRF was folded into Broken Access Control, which stays first.

| 2025 category | What it looks like | Where this guide handles it |
|---|---|---|
| A01 Broken Access Control | A route checks sign-in but not ownership; an SSRF reaching internal addresses | Chapter 7 (object-level checks, RLS), 8.6 |
| A02 Security Misconfiguration | Missing headers, debug pages, public buckets, permissive firewalls | 8.5, 8.10 |
| A03 Software Supply Chain Failures | A compromised library, base image or CI action | 8.9 |
| A04 Cryptographic Failures | Plain HTTP, home-made crypto, weak password hashing | 8.11, Chapter 7 |
| A05 Injection | SQL, template, command, and prompt injection | ORM parameters, template auto-escaping; prompt injection in Chapters 9 and 10 |
| A06 Insecure Design | No threat model; trust in the client | 8.3 |
| A07 Authentication Failures | Credential stuffing, weak sessions | Chapter 7, 8.7 |
| A08 Software or Data Integrity Failures | Unsigned artifacts; deserialising untrusted data | 8.9 |
| A09 Security Logging & Alerting Failures | Nobody notices the attack | 8.12 |
| A10 Mishandling of Exceptional Conditions | Errors that fail open, leak stack traces, or skip a check | Fail closed (RLS, CSRF); generic error pages |

For APIs, OWASP keeps a separate API Security Top 10 (Chapter 7's sources); its first entry, broken object-level authorization, is A01 again.

> **Fail closed.** When a check can't complete (the IdP is unreachable, the tenant isn't set, the token is missing), refuse. Ledgerline's RLS returns no rows without a tenant; its CSRF check refuses a POST without a token; its CSP is built without the IdP's address if the IdP can't be reached, which breaks sign-in rather than opening forms to anywhere.

---

## 8.5 Browser defences

A browser runs whatever your pages tell it to. A few response headers turn that around:

| Header | Purpose | Typical value |
|---|---|---|
| `Content-Security-Policy` | Which scripts, styles, frames, images and form targets are allowed | Start from `default-src 'none'` and add only what the page uses |
| `Strict-Transport-Security` | "Only ever HTTPS" for a year; no downgrade | `max-age=31536000; includeSubDomains` (HTTPS sites only) |
| `X-Content-Type-Options` | Don't guess types (a "text file" run as script) | `nosniff` |
| `X-Frame-Options` / CSP `frame-ancestors` | Who may put your page in a frame (clickjacking) | `DENY` / `'none'` |
| `Referrer-Policy` | What the next site learns of your URLs | `same-origin` or `strict-origin-when-cross-origin` |
| `Permissions-Policy` | Camera, microphone, location... off unless needed | `camera=(), microphone=(), geolocation=()` |
| `Cache-Control` | Keep pages with personal data out of shared caches | `no-store` for authenticated pages |

**CSP is the important one.** Template auto-escaping stops most XSS; the CSP is the second wall for the bug that slips through. A strict CSP with no `'unsafe-inline'` means injected `<script>` or `onclick=` simply doesn't run. The cost is that the pages can't use inline scripts or styles either, so move them into files (or use nonces for scripts). That's easy on day one and a project later.

**CSRF** is a different attack: another site's page submits a form to yours, and the browser attaches your user's cookies. Layers that stop it:

1. **`SameSite=Lax` (or `Strict`) cookies:** browsers don't send them on cross-site POSTs. This stops most CSRF.
2. **Origin checks:** browsers send `Sec-Fetch-Site` and `Origin` with POSTs. Refuse unless they say the request came from your own site.
3. **A CSRF token** in every form, tied to the session, that another site can't read. This covers old browsers and same-site attacks (a compromised subdomain).

**Cookies:** `HttpOnly` (no script access), `Secure` (HTTPS only), `SameSite`, and a narrow `Path`/`Domain`. Chapter 7's session cookie has all four.

> **Ledgerline** sends a CSP with no scripts at all, styles only from `/static/app.css` (every `style=` attribute moved into classes), forms only to itself and the sign-in provider, and `frame-ancestors 'none'`. The one page that may be framed is the invoice PDF, by Ledgerline's own invoice page. Every form carries a CSRF token derived from the session cookie, and every POST is refused if `Sec-Fetch-Site` or `Origin` points elsewhere. A ZAP baseline scan went from 9 warnings to 3, and the 3 are expected (part 2).

---

## 8.6 Trust the network only as far as you must

**Client addresses.** Behind a CDN and a load balancer, the TCP connection comes from the load balancer. The client's address is in `X-Forwarded-For`, but **the client can write that header too**; each proxy only appends what it saw. Trust exactly as many entries from the right as you have proxies, and no more. Taking the leftmost entry lets anyone choose their IP, which defeats per-IP limits, poisons logs and can bypass IP allow-lists.

> **Ledgerline's finding.** Until this chapter the container ran uvicorn with `--proxy-headers --forwarded-allow-ips "*"`. A test with a forged header (`X-Forwarded-For: 6.6.6.6, <real client>, <CloudFront>`) showed the app believing the client was `6.6.6.6`. The fix: the app counts back a fixed number of trusted hops (`TRUSTED_PROXY_HOPS=2` on AWS: CloudFront, then the load balancer), and on staging 32 sign-in starts with 32 different forged addresses still hit the limit after 30.

**Server-side requests (SSRF).** If your server fetches URLs that users supply (webhooks, "import from URL", link previews, an agent's browsing tool), an attacker will ask it for `http://169.254.169.254/` (cloud credentials), `http://localhost:5432` or your internal services. Defend by allow-listing schemes and hosts, resolving the name yourself and refusing private, loopback and link-local addresses (and re-checking after redirects), using a separate egress proxy for user-directed fetches, and requiring IMDSv2 on EC2. Since 2025 OWASP files SSRF under Broken Access Control.

**Egress.** Decide what your containers may connect *out* to. "Anything" lets malware phone home and lets SSRF reach anything. Even narrowing to HTTPS removes whole classes of exfiltration; destination allow-lists need an egress proxy or VPC endpoints.

> **Ledgerline's** app containers could connect anywhere on any port. Now: TCP 443 anywhere (AWS APIs, the model provider, the sign-in provider) and Postgres to the database's security group only. Ledgerline fetches no user-supplied URLs, which removes SSRF as a category, and the model's tool calls (Chapter 10) won't get a general web fetch.

---

## 8.7 Abuse limits

Rate limits protect three things: accounts (password guessing), money (expensive operations such as model calls) and availability.

| Layer | Good for | Watch for |
|---|---|---|
| Edge / WAF rate rules | Floods by IP, before they reach you | Coarse; shared IPs (offices, mobile carriers) look like one client |
| The identity provider | Lockouts and password-guessing protection | Only covers its own sign-in pages |
| Your app | Per-user and per-tenant limits that know who's who: uploads, exports, model calls | Needs shared state across instances (database or Redis), not a counter in memory |
| Quotas and budgets | Per-tenant monthly limits on expensive work | Tie to plans and billing (Chapter 12) |

Key limits on something an attacker can't easily change: the authenticated user or tenant first, the client IP (correctly derived, 8.6) for anonymous routes. Return `429` with `Retry-After`. Log limit hits: a burst is a signal (8.12).

> **Ledgerline** limits sign-in starts to 30 per 10 minutes per client address, and uploads to 120 per hour per user, counted in a Postgres table shared by every app task (fixed windows; a stale-window cleanup runs in the worker's sweep). Uploads are the expensive path: each costs a model call. Per-tenant monthly budgets come with pricing and billing (Chapter 12).

---

## 8.8 Secrets

A secret is anything that grants access: database passwords, API keys, signing keys, OAuth client secrets, session-signing keys, webhook secrets.

- **Never in code, images, logs or CI output.** Scan the whole git history for them (gitleaks runs in Ledgerline's CI from Chapter 2).
- **A secrets store** (AWS SSM Parameter Store or Secrets Manager, Vault, GCP Secret Manager), injected at start-up or fetched at runtime; access granted per service.
- **Generated, not chosen.** Long random values, created by a machine. Generated secrets shouldn't pass through Terraform state either (Ledgerline uses ephemeral resources and write-only arguments).
- **Fewer long-lived secrets.** Short-lived credentials from workload identity (IAM roles for tasks, OIDC federation for CI) beat stored keys. Ledgerline's account blocks CI's AWS OIDC federation, so it uses one key per environment with minimal permissions (Chapter 4), while signing (8.9) uses GitHub's own short-lived identity.
- **Rotation that's been practised.** Know how to rotate each secret, and how long it takes. Rotate at once when a secret has been exposed, even "only in a chat" or "only on my screen".

> **Ledgerline's demo exposed a secret by accident.** `Read-Host` shows what's typed, so a staging password appeared on screen and in a log. It was rotated: a new 24-character random value generated straight into SSM (never displayed), then a forced redeploy so the app read it. The deploy runbook now reads the password from SSM into a variable rather than asking anyone to type it (part 2, 8B.4).

---

## 8.9 The software supply chain

Everything that goes into your artifact is code you run: libraries, the base image, OS packages, build tools, CI actions, Terraform providers. Each can be compromised at the source, at a registry, or in your pipeline.

**The March 2026 Trivy incident** is the lesson in one story. Attackers used a compromised credential to publish a malicious Trivy release, force-push 76 of 77 version tags of `aquasecurity/trivy-action` and all 7 tags of `aquasecurity/setup-trivy` to credential-stealing code. Anyone who referenced the action by tag (`@v0.x`) ran the malware with their CI secrets; anyone pinned to a commit SHA did not. The compromised action tags were live for about 12 hours. A security scanner became the attack.

The controls, roughly in order of value:

| Control | Stops | How |
|---|---|---|
| **Pin everything** | A tag or "latest" moved to bad code | Lock files with hashes for libraries; actions pinned by commit SHA; base images by digest |
| **Fewer, better dependencies** | Typosquats, abandoned packages | Review new dependencies like new code; prefer well-maintained ones |
| **Known-vulnerability scanning** | Shipping a library or OS package with a public CVE | `pip-audit`, Trivy, Grype, Dependabot alerts; on every build, failing on what you can fix |
| **Update automation with a cooldown** | Old versions piling up; adopting a poisoned release on day one | Dependabot or Renovate, grouped, weekly, waiting a few days after each release |
| **SBOM** | "Are we affected by CVE-X?" taking days | Generate one per image (CycloneDX or SPDX), keep it with the release |
| **Signing and verification** | An image that CI didn't build reaching production | Sign in CI (Sigstore cosign, keyless); verify at deploy, ideally at the platform (admission control) |
| **Least-privilege CI** | A compromised step stealing everything | Read-only default token; secrets only in the jobs that need them; no secrets for pull requests from forks |
| **Provenance (SLSA)** | Builds you can't reproduce or trace | Signed statements of how and from what an artifact was built; aim for SLSA build level 2–3 over time |

> **Dependabot's cooldown.** Since July 2026 Dependabot waits at least three days after a release before proposing it, because malicious releases are usually caught and pulled within days. Security updates aren't delayed. Ledgerline sets seven days.

> **Ledgerline's CI** already pinned actions by SHA and ran gitleaks and pip-audit. Chapter 8 adds: Trivy installed from its release tarball with a pinned version and checksum (not through `trivy-action`); an image scan that fails on fixable CRITICAL findings and lists HIGH ones; a CycloneDX SBOM kept 90 days as a build artifact; keyless cosign signing in its own job (the only one with `id-token: write`); the SBOM attached as a signed attestation; deploys by digest that first verify the signature names exactly `ci.yml` on `main`; and Dependabot for pip, Docker, Actions and Terraform. The first scan found one fixable HIGH (zlib) in the Debian base image (part 2).

**Keyless signing, briefly.** The CI job asks GitHub for a short-lived OIDC identity token. Sigstore's certificate authority (Fulcio) issues a certificate, valid for about 10 minutes, naming the workflow and branch; cosign signs the image digest; the signature is recorded in a public transparency log (Rekor). Verifying means checking the signature, the certificate chain, the log entry, and that the identity is the one you expect. No signing key exists to leak.

> **What verification inside the same pipeline buys.** It proves the image came from `main`'s CI and hasn't changed since. It doesn't stop someone who controls the pipeline itself. The stronger form is enforcement by the platform: an admission controller (Kubernetes) or a deploy service that refuses unsigned images whoever asks. Customers and auditors can also verify independently.

---

## 8.10 Infrastructure scanning and accepted risk

Infrastructure as code can be scanned like code: public buckets, open security groups, unencrypted resources, missing logs. Tools: Trivy (`trivy config`), Checkov, KICS, tfsec's successor in Trivy.

Run one in CI, failing on HIGH and CRITICAL. The first run will find things, and some findings will be correct but not worth fixing yet. The discipline that matters is **how you accept risk**:

- **Written down, in the repo,** next to the code.
- **With a reason** a newcomer can evaluate.
- **Scoped** to the file or resource, not the whole rule.
- **With an expiry date,** after which CI fails again and someone must decide afresh.

> **Ledgerline's** first Terraform scan reported 17 HIGH or CRITICAL findings under 6 rules: plain HTTP from CloudFront to the load balancer (no certificate without a domain yet), an internet-facing load balancer (CloudFront needs a public origin; the fix is a VPC origin), no WAF, unrestricted egress, an unencrypted SNS topic (alarm messages carry no data), and S3 with AWS-managed rather than customer-managed keys. One was fixed (egress narrowed to HTTPS and Postgres); all six are listed in `.trivyignore.yaml` with reasons and 2027 expiry dates.

---

## 8.11 Encryption

- **In transit:** HTTPS everywhere a user goes, with HSTS; TLS to managed databases and between services where traffic leaves a private network. Watch the hops behind the CDN (Ledgerline's CloudFront-to-ALB hop is the accepted exception above).
- **At rest:** managed services encrypt by default (S3, RDS, EBS, SQS). Customer-managed keys (KMS) add key policies, rotation control and an audit trail of key use, at about $1 per key per month plus requests; reach for them when a contract or a separation-of-duties need asks.
- **Application-level:** encrypt specific fields (bank details, national IDs) when even database administrators shouldn't read them; per-tenant keys are an enterprise ask (Chapter 7).
- **Don't invent crypto.** Use the platform's KMS, libsodium, or your language's vetted libraries. Hash passwords with a slow hash, or better, don't store them (Chapter 7).

---

## 8.12 Audit logs and detection

Two different logs:

- **Audit trail:** who did what to which business object, kept for years, readable by customers' admins. Ledgerline has had one since Chapter 1 (`audit_events`: sign-ins, approvals, changes, identity unlinking).
- **Security telemetry:** sign-in failures, rate-limit hits, permission denials, CSRF refusals, admin actions, changes to infrastructure (CloudTrail), all with request IDs and tenant (Chapter 3).

Then detection: alerts on patterns, not just errors. A spike in 401s, 403s or 429s; sign-ins from new countries for an admin; a CI user's key used from an unusual address; a change to IAM outside Terraform. On AWS, GuardDuty analyses CloudTrail, VPC flow logs and DNS logs for known-bad behaviour; it has a 30-day free trial per account and Region, then charges by volume. Security Hub collects findings against benchmarks.

> **Keep audit logs out of reach** of the people and systems they record: a separate account or write-once storage (S3 Object Lock) as you grow.

---

## 8.13 When something goes wrong

Incident response for security follows Chapter 3's shape (detect, triage, mitigate, communicate, learn) with extra steps:

1. **Contain** before you investigate: revoke the key, end sessions, block the address, take the feature offline.
2. **Preserve evidence:** snapshot, export logs, don't "clean up" yet.
3. **Rotate** everything the attacker could have seen, not just what you know they used.
4. **Assess impact:** which tenants, which data, which time window. Tenant IDs in logs (Chapter 3) make this hours instead of weeks.
5. **Notify:** customers per contract, regulators per law (GDPR's 72 hours to the authority, for example; Chapter 13), and your insurer.
6. **Post-incident review,** blameless, with actions tracked to done.

Write a one-page runbook for the likely ones before you need it: "a secret was exposed", "a dependency we use was compromised" (was the bad version in any build? the SBOMs answer that), "a customer reports seeing another tenant's data".

Publish a way to report vulnerabilities: a `SECURITY.md` and `security.txt` with an address, and a promise not to pursue good-faith researchers.

---

## 8.14 Security testing

| Kind | What | When |
|---|---|---|
| Lint and SAST | Ruff's security rules, Semgrep, CodeQL | Every push |
| SCA | Known-vulnerable dependencies (pip-audit, Trivy, Dependabot) | Every push, plus daily for new CVEs |
| Secret scanning | gitleaks; GitHub push protection | Every push |
| IaC scanning | Trivy config, Checkov | Every push |
| DAST | OWASP ZAP baseline (passive) on a running app; full scans with sign-in in a test environment | Each release; weekly |
| Security unit tests | CSRF refused, forged headers ignored, other tenants' IDs 404, headers present | Every push |
| Penetration test | People attacking deliberately, with a report | Before enterprise sales; yearly; after big changes |
| Bug bounty | The public, paid per finding | Once the basics are solid and someone can triage reports |

Tests that encode the attack are the ones that keep working: Ledgerline's `test_security.py` checks the CSP on pages and error pages, that no template has inline styles or scripts, that a forged `X-Forwarded-For` is ignored, that rate limits trip, and that POSTs without a token or from another origin get 403.

---

## 8.15 By stage

| Stage | Web app | Supply chain | Operations |
|---|---|---|---|
| MVP | Framework defaults; auto-escaping; secure cookies; secrets out of code | Lock files; secret scanning; pip-audit or equivalent | HTTPS; a security contact address |
| Early production | Strict CSP, security headers, CSRF layers, correct client IPs, rate limits; ZAP in the release routine | Actions pinned by SHA; image scanning; SBOMs; Dependabot with a cooldown; IaC scanning with expiring exceptions | Audit trail; alerts on 401/403/429 spikes; a "secret exposed" runbook |
| Growth | WAF with managed rules; per-tenant quotas; SSRF-safe fetchers | Signing verified by the platform; SLSA provenance; private package mirror | GuardDuty/Security Hub; yearly pen test; incident drills |
| Enterprise | Customer-specific controls (IP allow-lists, session policies) | Reproducible builds; vendor security reviews | SOC 2 / ISO 27001 evidence (Chapter 13); 24/7 response; bug bounty |

---

## 8.16 By product type

- **B2B SaaS:** tenant isolation is the security property customers care most about (Chapter 7); expect questionnaires and pen-test requests early. SSO and audit logs are security features in buyers' eyes.
- **AI and agentic products:** new inputs to distrust: model output (treat as untrusted text, escape it, never run it), prompts hidden in documents (prompt injection, Chapters 9 and 10), tool calls (authorize each like a user action; no general-purpose web fetch or shell), and spending (per-tenant model budgets are an abuse limit). Model and dataset files are supply chain too: pin and verify them.
- **Internal tools:** behind the company's SSO and network controls, but still with CSRF and authorization checks: internal tools are where attackers go after the first foothold.
- **Consumer products:** bots, credential stuffing and fraud dominate: CAPTCHAs or proof-of-work, breached-password checks, device signals, and a WAF.

---

## 8.17 Challenges and watch-outs

- **Scanning without fixing.** A dashboard of 400 findings nobody owns. Fail builds only on what's fixable and important; track the rest with owners.
- **Exceptions that never expire.** An ignore file that only grows.
- **Trusting `X-Forwarded-For`,** or any header a client can set.
- **"We have SameSite cookies, so no CSRF defence"** (subdomains, old browsers, GET requests that change state).
- **A CSP with `'unsafe-inline'`,** which keeps the header and loses most of the protection.
- **Actions and images referenced by tag.**
- **Security tools with broad access:** scanners in jobs that hold deploy keys.
- **Secrets typed, pasted or echoed** into terminals, chats and tickets.
- **Signing without verifying,** or verifying "any valid signature" instead of a specific identity.
- **Pen tests as a once-a-year ritual** with nothing in between.
- **Signals to act on:** a customer security questionnaire; a dependency in the news; a spike in 401/403/429; an engineer asking for production access "just this once"; any new feature that fetches URLs, runs code or calls tools.

---

## 8.18 Exercise

For each situation, what's the risk, and what would you do?

1. The app limits sign-in attempts per IP, taking the address from `X-Forwarded-For`'s first entry.
2. A new marketing page needs an analytics script from a third-party domain.
3. Trivy reports a HIGH vulnerability in an OS package, "fixed in" a newer Debian release.
4. A workflow uses `some-org/deploy-action@v2` and has your production AWS key in its environment.
5. An engineer pastes a database URL with its password into the team chat to debug.
6. A customer asks "how do you know the code in production is the code you reviewed?"

<details>
<summary>Suggested answers</summary>

1. **Risk:** the client writes that entry, so the limit can be dodged with a header, and logs record a forged address. **Do:** count back a fixed number of trusted proxies from the right; test it with a forged header.
2. **Risk:** any script you include can read every page it runs on. **Do:** keep it off authenticated pages; if it must be there, add only its exact origin to `script-src` with a nonce or hash, and prefer a self-hosted, privacy-respecting option.
3. **Risk:** a known flaw in the image. **Do:** rebuild on the patched base image (pinned digest, updated by Dependabot) or upgrade the package in the Dockerfile; if not exploitable in your use, record it as accepted with an expiry.
4. **Risk:** whoever controls that tag controls your production account. **Do:** pin to a commit SHA, give the job only the permissions it needs, and keep production credentials in a protected environment that requires approval.
5. **Risk:** the password is now in chat history, notifications, maybe backups. **Do:** rotate it now, then talk about how to share access safely (temporary credentials, a secrets tool).
6. **Answer:** reviewed pull requests merge to `main`; only `main`'s CI builds images; each image is signed by that workflow's identity, with its SBOM attested; deploys refuse anything else, and they can verify the signature themselves.

</details>

---

## 8.19 Cost

November 2026 list prices, US regions.

| Item | Approx. monthly | Notes |
|---|---|---|
| Headers, CSP, CSRF, rate limits | Nothing | A day of work, less on a new app |
| OWASP ZAP, Trivy, cosign, gitleaks, pip-audit, Dependabot | Free | Open source / included with GitHub; CI minutes only |
| Sigstore public instance (Fulcio, Rekor) | Free | Signatures and identities are public; private instances exist for those who need them |
| AWS WAF | $5 per web ACL + $1 per rule + $0.60 per million requests | Managed rule groups $1 each; Bot Control extra |
| AWS KMS customer-managed key | About $1 per key, plus requests | Only where a contract or policy asks |
| GuardDuty | Free for 30 days per account and Region, then by volume | Usually small for a small account; check its usage page during the trial |
| Penetration test | Thousands to tens of thousands of dollars | Per engagement; scope it to what customers ask about |

The expensive line isn't here: a breach, or a deal lost for lack of answers.

---

## 8.20 Demo

[Part 2, Security in practice](08b-security-demos.md):

1. **Harden the web app:** a ZAP baseline before and after; a strict CSP, security headers, CSRF tokens and origin checks, correct client addresses behind CloudFront, and rate limits, each proven with a command.
2. **Supply chain in CI:** an infrastructure scan with expiring exceptions, an image scan and SBOM, keyless signing, verification before every deploy, and Dependabot; then verifying the signature by hand from a laptop, with the right identity and the wrong one.

---

## 8.21 Checklist

Your security is in good shape when:

- [ ] There's a one-page threat model per system, updated when the architecture changes
- [ ] Pages send a strict CSP (no `'unsafe-inline'`), HSTS, `nosniff`, framing protection and `no-store` for private data
- [ ] State-changing requests need same-origin headers and a CSRF token; cookies are `HttpOnly`, `Secure`, `SameSite`
- [ ] Client addresses come from a fixed number of trusted proxies; forged headers are ignored (tested)
- [ ] Sign-in and expensive operations are rate-limited per user or correctly derived IP
- [ ] Outbound connections are limited; user-supplied URLs are fetched only through SSRF-safe code
- [ ] Secrets live in a secrets store, are generated, are scanned for in git history, and have a practised rotation
- [ ] Dependencies, base images and CI actions are pinned; known-vulnerability scans fail builds on fixable critical findings
- [ ] Every image has an SBOM and a signature; deploys verify a specific signer identity
- [ ] Dependency updates are automated, grouped, and wait a few days after release
- [ ] Infrastructure is scanned in CI; accepted risks are written down with reasons and expiry dates
- [ ] Security events are logged with request and tenant IDs, and unusual patterns alert someone
- [ ] There's a runbook for exposed secrets and compromised dependencies, and a published way to report vulnerabilities
- [ ] A DAST scan runs each release, and security tests encode the attacks you've defended against

---

**Next:** Chapter 9, LLMs in production: choosing and routing models, prompts as code, structured output, evaluation, cost and latency, prompt injection, and keeping model inputs and outputs inside tenant boundaries.

Sources: [OWASP Top 10:2025](https://owasp.org/Top10/2025/0x00_2025-Introduction/) · [Trivy advisory GHSA-69fq-xp46-6x23](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23) · [Aqua: Trivy supply chain attack](https://www.aquasec.com/blog/trivy-supply-chain-attack-what-you-need-to-know/) · [Dependabot default cooldown (GitHub changelog)](https://github.blog/changelog/2026-07-14-dependabot-version-updates-introduce-default-package-cooldown/) · [Dependabot options reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference) · [Sigstore: cosign and OCI referrers](https://docs.sigstore.dev/cosign/) · [AWS WAF pricing](https://aws.amazon.com/waf/pricing/) · [GuardDuty pricing](https://docs.aws.amazon.com/guardduty/latest/ug/guardduty-pricing.html) · [MDN: Content-Security-Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy) · [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
