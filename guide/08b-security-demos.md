# Chapter 8, part 2: Security in practice

> **In this part:** Ledgerline's web app is hardened against an OWASP ZAP scan: a strict content security policy, security headers, CSRF tokens and origin checks, correct client addresses behind CloudFront, and rate limits. On the way, a test finds that a forged header could choose the client's IP address. Then CI gains a supply chain: an infrastructure scan with expiring exceptions, an image vulnerability scan, an SBOM, keyless signing, and verification before every deploy. For each: the steps, the real results, and what went wrong. Concepts are in [part 1](08-security.md).

Run in November 2026: locally on a Windows laptop (PowerShell 5, Docker Desktop) and in a Linux sandbox (ZAP 2.16.1, Trivy 0.76.2), on GitHub Actions, and on AWS in `ap-southeast-2` (Sydney).

---

## Contents

- [8B.1 What changed in the code](#8b1-what-changed-in-the-code)
- [8B.2 Demo 1: harden the web app](#8b2-demo-1-harden-the-web-app)
- [8B.3 Demo 2: supply chain in CI](#8b3-demo-2-supply-chain-in-ci)
- [8B.4 What went wrong](#8b4-what-went-wrong)
- [8B.5 Troubleshooting](#8b5-troubleshooting)
- [8B.6 Checklist](#8b6-checklist)

---

## 8B.1 What changed in the code

| File | What it does |
|---|---|
| [`app/security.py`](../ledgerline/app/security.py) | New. `security_headers()`: a CSP with no scripts, styles from `'self'`, `form-action` limited to Ledgerline and the sign-in provider (read from its discovery document, cached), `frame-ancestors 'none'`; `nosniff`, `Referrer-Policy`, `Permissions-Policy`, cross-origin policies, `X-Frame-Options`, HSTS when deployed, `Cache-Control: no-store` except `/static`. The invoice PDF may be framed by Ledgerline's own page only. `client_ip()`: the address `TRUSTED_PROXY_HOPS` entries from the right of `X-Forwarded-For`. `csrf_token()` and `csrf_protect()`: unsafe methods need `Sec-Fetch-Site` same-origin (or absent), a matching `Origin` if sent, and, when signed in, the form's token. `rate_limit()`: fixed windows counted in Postgres, `429` with `Retry-After`. |
| [`app/static/app.css`](../ledgerline/app/static/app.css), templates | The CSS moved out of `base.html` into a file, every `style=` attribute replaced by a class, so the CSP needs no `'unsafe-inline'`. Every POST form carries `csrf_token`. |
| [`app/main.py`](../ledgerline/app/main.py) | Mounts `/static`; adds the headers middleware; `csrf_protect` as an app-wide dependency; rate limits on the development login, `/auth/login` and `/auth/callback` (30 per 10 minutes per client address) and uploads (120 per hour per user). |
| [`app/models.py`](../ledgerline/app/models.py), migration [`0006`](../ledgerline/migrations/versions/0006_rate_limits.py) | `rate_limits` (key, window start, count). The worker's sweep deletes old windows. |
| [`Dockerfile`](../ledgerline/Dockerfile) | uvicorn without `--proxy-headers --forwarded-allow-ips "*"` (8B.4), and with `--no-server-header`. Optionally, the base image pinned by digest. |
| [`tests/test_security.py`](../ledgerline/tests/test_security.py), [`tests/helpers.py`](../ledgerline/tests/helpers.py) | 13 tests: CSP on pages, errors and sign-in; no inline styles or scripts in any template; the stylesheet served; the IdP's origin in `form-action`; framing rules for the PDF; forged `X-Forwarded-For` ignored (parametrized); sign-in and upload limits; POSTs without a token, with a forged token, or from another site refused. Signed-in test clients add the token like a real page would. |
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Trivy installed from its release with a pinned version and SHA-256. Security job: `trivy config` on `infra/`. Build job: image scan (fail on fixable CRITICAL, list HIGH), CycloneDX SBOM uploaded as an artifact; the pushed image named by **digest**. New **Sign the image** job (the only one with `id-token: write`): `cosign sign` and `cosign attest --type cyclonedx`. Both deploy jobs: `cosign verify` and `verify-attestation` for exactly `ci.yml@refs/heads/main` before anything changes. |
| [`.trivyignore.yaml`](../ledgerline/.trivyignore.yaml) | Six accepted infrastructure findings, each scoped to files, with a statement and an expiry date. |
| [`.github/dependabot.yml`](../.github/dependabot.yml) | Weekly grouped updates for pip, Docker, GitHub Actions and Terraform, with a 7-day cooldown. |
| [`infra/`](../ledgerline/infra/) | App egress narrowed to TCP 443 and Postgres to the database (was all traffic); `TRUSTED_PROXY_HOPS=2`; ECR keeps 60 images (signatures and SBOMs are stored beside each); `ecr:ListImages` for the staging CI user; read-only ECR access for production's CI user, to verify before promoting. |
| [`scripts/deploy_ecs.sh`](../ledgerline/scripts/deploy_ecs.sh), [`scripts/upload_many.py`](../ledgerline/scripts/upload_many.py) | The deploy script recognises digest references (`/ledgerline@sha256:…`); the upload script reads the CSRF token from the page like a browser. |

---

## 8B.2 Demo 1: harden the web app

**Cost:** free. About 30 minutes.

### Steps

From the repository root, with the Chapter 7 stack still running on port 8007:

```powershell
# 1. ZAP baseline scan of the OLD code (passive: spiders and inspects, doesn't attack)
docker run --rm -t ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t http://host.docker.internal:8007 -I

# 2. The new code; migration 0006 runs in the migrate service
cd ledgerline
docker compose up -d --build

# 3. Headers, then POSTs from another site and from nowhere
curl.exe -sI http://127.0.0.1:8007/login
curl.exe -s -o NUL -w "%{http_code}`n" -X POST -H "Origin: https://evil.example" http://127.0.0.1:8007/logout
curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://127.0.0.1:8007/logout

# 4. In the browser with F12 > Console open: sign in through Keycloak, upload,
#    approve, sign out. Everything should work, with no CSP errors in the console.

# 5. The sign-in limit (last: it locks sign-in for up to 10 minutes)
1..32 | % { curl.exe -s -o NUL -w "%{http_code} " http://127.0.0.1:8007/auth/login }

# 6. ZAP again, on the new code
docker run --rm -t ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t http://host.docker.internal:8007 -I
```

`-I` keeps ZAP from returning an error code on warnings; drop it to use the scan as a CI gate.

### Results

**ZAP, before and after:**

| Warning | Before | After |
|---|---|---|
| Content Security Policy (CSP) Header Not Set | ✗ | fixed |
| Missing Anti-clickjacking Header | ✗ | fixed |
| X-Content-Type-Options Header Missing | ✗ | fixed |
| Permissions Policy Header Not Set | ✗ | fixed |
| Absence of Anti-CSRF Tokens | ✗ | fixed |
| Session Management Response Identified | ✗ | ✗ (informational: ZAP noticed the session cookie) |
| User Controllable HTML Element Attribute | ✗ | ✗ (`?next=` is echoed into a hidden field, escaped) |
| Non-Storable Content | ✗ | ✗ (that's `no-store` working) |
| Cross-Origin-Embedder-Policy Header Missing | ✗ | ✗ (only needed for pages that require cross-origin isolation) |
| **Total** | **9 warnings, 59 passes** | **4 warnings, 64 passes** |

A third scan, run while sign-in was rate-limited, showed 3: ZAP's sign-in requests got `429`, so it never saw a session cookie. That's not a fix; it's a reminder to read what a scanner actually visited. In the sandbox, the same scan of the earlier code (before CSRF tokens) left "Absence of Anti-CSRF Tokens" as the last medium finding, which is what led to adding them.

**Headers** (`curl -I` sends `HEAD`, which the route doesn't allow, so the status is `405`; the headers are on error responses too, which is the point):

```
content-security-policy: default-src 'none'; style-src 'self'; img-src 'self'; frame-src 'self'; form-action 'self' http://localhost:8180; base-uri 'none'; frame-ancestors 'none'
x-frame-options: DENY
x-content-type-options: nosniff
referrer-policy: same-origin
permissions-policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()
cross-origin-opener-policy: same-origin
cross-origin-resource-policy: same-origin
cache-control: no-store
```

`http://localhost:8180` in `form-action` is local Keycloak, read from its discovery document: "Sign in" and "Sign out" end up there, and browsers apply `form-action` to redirects after a form submission.

**CSRF:** the POST with `Origin: https://evil.example` got **403**; the same POST without an `Origin` got **303** (curl isn't a browser; with no session there's no token to check). The signed-in cases (missing token, forged token, cross-site `Sec-Fetch-Site`) are covered by the tests.

**Browser:** sign-in through Keycloak, upload, approval and sign-out all worked; no CSP violations in the console.

**Rate limit:** `303` thirty times, then `429 429`.

---

## 8B.3 Demo 2: supply chain in CI

**Cost:** nothing beyond running staging. About 45 minutes, most of it CI.

The order is the usual one: **Terraform first, then push.**

```powershell
# 1. Sign in; start the database
aws login --profile ledgerline
$Env:AWS_PROFILE = "ledgerline"
aws rds start-db-instance --db-instance-identifier ledgerline-staging --query "DBInstance.DBInstanceStatus" --output text
aws rds wait db-instance-available --db-instance-identifier ledgerline-staging

# 2. (Optional) pin the base image by digest; Dependabot then proposes new digests weekly
$d = (docker buildx imagetools inspect python:3.12-slim-trixie --format '{{json .Manifest.Digest}}') -replace '"',''
(Get-Content Dockerfile) -replace 'FROM python:3.12-slim-trixie AS base', "FROM python:3.12-slim-trixie@$d AS base" | Set-Content Dockerfile
Select-String "^FROM python" Dockerfile    # must show @sha256:<64 hex>; an empty $d breaks the build

# 3. Commit (check git status first), don't push yet; then Terraform
cd infra\staging
terraform plan  -var alarms_enabled=false
terraform apply -var alarms_enabled=false
cd ..\..

# 4. Push, and watch the run
git push

# 5. When deploy-staging is green: forged X-Forwarded-For must not dodge the limit.
#    The site password goes from SSM into a variable, never on screen.
$url = "<staging URL>"
$pw = aws ssm get-parameter --name /ledgerline/staging/site_password --with-decryption --query Parameter.Value --output text
1..32 | % { curl.exe -s -o NUL -w "%{http_code} " -u "ledgerline:$pw" -H "X-Forwarded-For: 10.0.0.$_" "$url/auth/login" }
Remove-Variable pw

# 6. Verify the signature yourself: the right identity, then the wrong branch
#    (cosign.exe from the sigstore/cosign release page; compare its SHA-256 with cosign_checksums.txt)
$img = aws ecs describe-task-definition --task-definition ledgerline-staging-app --query "taskDefinition.containerDefinitions[?contains(image, '/ledgerline@')].image | [0]" --output text
$tok = aws ecr get-login-password --region ap-southeast-2
& $HOME\cosign.exe verify --registry-username AWS --registry-password $tok --certificate-identity "https://github.com/<owner>/<repo>/.github/workflows/ci.yml@refs/heads/main" --certificate-oidc-issuer https://token.actions.githubusercontent.com $img
& $HOME\cosign.exe verify --registry-username AWS --registry-password $tok --certificate-identity "https://github.com/<owner>/<repo>/.github/workflows/ci.yml@refs/heads/dev" --certificate-oidc-issuer https://token.actions.githubusercontent.com $img
Remove-Variable tok

# 7. Pause staging
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 0 --query "service.desiredCount"
aws rds stop-db-instance --db-instance-identifier ledgerline-staging --query "DBInstance.DBInstanceStatus" --output text
```

### Results

**Terraform:** `Apply complete! Resources: 4 added, 2 changed, 3 destroyed.`

| Change | Why |
|---|---|
| Egress rule "All outbound" destroyed; "HTTPS outbound" and "Postgres to the database only" created | Narrower egress (part 1, 8.6) |
| ECR lifecycle policy replaced (20 → 60 images) | Terraform recreates a lifecycle policy to change it; signatures and SBOMs are stored beside images |
| App task definition replaced | `TRUSTED_PROXY_HOPS=2` added |
| Cognito user pool updated | The NIST-aligned password policy from Chapter 7: 15 characters, no composition rules |
| Staging CI user's policy updated | `ecr:ListImages` |

The app kept working after egress was narrowed: database, S3, SQS, Cognito and CloudWatch are all HTTPS or Postgres.

**CI:** every job green. What the new steps showed:

- **Infrastructure scan:** passed; the six accepted findings were suppressed by `.trivyignore.yaml`, nothing else at HIGH or CRITICAL.
- **Image scan:** 87 Debian packages and every installed Python package. Python: no HIGH or CRITICAL. Debian: **one HIGH, fixable**:

  ```
  Total: 1 (HIGH: 1, CRITICAL: 0)
  zlib1g  CVE-2026-85091  HIGH  fixed  1:1.3.dfsg+really1.3.1-1+b1 -> 1:1.3.dfsg+really1.3.1-1+deb13u1
          zlib: Heap buffer overflow in gz_vacate() allows arbitrary code execution
  ```

  The build passed because the gate is CRITICAL only (8B.4 discusses that choice).
- **SBOM:** `sbom` artifact on the run page, `sbom.cdx.json` (CycloneDX).
- **Sign the image:** signed and attested. The run summary records the digest and the signer identity.
- **Deploy to staging:** "Refuse an image this workflow didn't sign" passed, then the migration (0006) and rollout. The task definition now names the image by digest: `…/ledgerline@sha256:fad95b5b…`.

**Forged `X-Forwarded-For`:** `303` thirty times, then `429 429`, although every request claimed a different address. CloudFront and the load balancer append the real addresses on the right, and the app counts two from the right.

**Verifying by hand.** The right identity:

```
Verification for …/ledgerline@sha256:fad95b5b… --
The following checks were performed on each of these signatures:
  - The cosign claims were validated
  - Existence of the claims in the transparency log was verified offline
  - The code-signing certificate was verified using trusted certificate authority certificates
[{"critical":{…"type":"https://cyclonedx.org/bom"}…},{"critical":{…"type":"https://sigstore.dev/cosign/sign/v1"}…}]
```

Two signed items on the image: the SBOM attestation and the signature. The wrong branch:

```
Error: no matching attestations: failed to verify certificate identity: no matching CertificateIdentity found,
last error: expected SAN value "…/ci.yml@refs/heads/dev", got "…/ci.yml@refs/heads/main"
```

That refusal is the whole point: a valid signature from the wrong workflow or branch doesn't count.

---

## 8B.4 What went wrong

> **The app believed any client IP it was told.** The container had run uvicorn with `--proxy-headers --forwarded-allow-ips "*"` since Chapter 2, a common snippet. A sandbox test sent `X-Forwarded-For: 6.6.6.6, 203.0.113.9, 130.176.1.1` (forged, client, CloudFront): the app's `request.client` was `6.6.6.6`. uvicorn, told to trust every proxy, takes the leftmost address, which the client wrote. Until then it only affected logs; with per-IP rate limits it would have made them useless. Fix: no proxy headers in uvicorn, and `client_ip()` counting `TRUSTED_PROXY_HOPS` from the right. On staging, 32 different forged addresses still hit the limit after 30.

> **The first curl checks all printed `000`.** `000` is curl's "no response at all", not a status. They had run seconds after `docker compose up`, while the API was still starting. A few minutes later the same checks returned 403 and 303, against both `127.0.0.1` and `localhost`. Give a freshly started container a moment, or check `docker compose ps` first.

> **Locally, `localhost` and `127.0.0.1` shared one limit.** After 30 sign-in starts against `127.0.0.1`, the run against `localhost` was all `429`. Docker Desktop forwards both through its own gateway, so the app sees one address for every local request. Behind a NAT, a corporate proxy or a carrier, many real people share one address too: per-IP limits must be generous, and per-user limits do the fine-grained work.

> **PowerShell 5 and Docker Desktop fought the ECR login.** `aws ecr get-login-password | docker login …` failed with `400 Bad Request`: PowerShell 5 re-encodes text piped to native programs. Through `cmd /c` the login succeeded but couldn't be saved: `The stub received bad data`, the Windows credential store rejecting ECR's long token. An empty `DOCKER_CONFIG` didn't help (Docker Desktop still chose the credential store). What worked: skip Docker entirely and give cosign the token directly, `--registry-username AWS --registry-password $tok`.

> **A password went on screen, and turned out to be wrong.** The first forged-header test asked for the site password with `Read-Host`, which echoes what's typed, so it ended up in the terminal and in a pasted log. Every request returned `401`: the gate rejected it before the rate limit was involved. SSM showed the stored value was 5 characters long, not Terraform's 24, so it had been changed by hand at some point. Both problems were fixed the same way: a new random 24-character value generated straight into SSM (`RandomNumberGenerator`, never displayed), `--force-new-deployment` so the app re-read it, and the runbook now reads the password from SSM into a variable. A test that never reaches the code under test proves nothing; check that the responses are the ones you expect (`303` here) before reading the interesting ones.

> **The image scan found a fixable HIGH and still passed.** zlib in the Debian base image had a heap overflow with a fix already in Debian (`+deb13u1`). The gate fails on fixable CRITICAL only, a deliberate choice: a red build for every HIGH in a base image teaches people to ignore red builds. But this one is fixable, so it shouldn't wait for the next base-image release. The options: let Dependabot's weekly base-image update pick it up (with a pinned digest), or add `apt-get upgrade` for security updates in the Dockerfile's base stage, trading a little reproducibility for faster patches. Either way, someone should own HIGH findings with a deadline (say, 14 days).

> **Not using `trivy-action` was the right call.** While writing the workflow, the obvious step was `aquasecurity/trivy-action`. Checking it turned up the March 2026 compromise (part 1, 8.9). The workflow installs Trivy from its GitHub release with a pinned version and checks the tarball's SHA-256, so neither a moved tag nor a replaced release can change what runs.

> **Some things couldn't be tested before the push.** The sandbox had no Docker engine and couldn't reach Trivy's vulnerability database, so the image scan, the signing and the verification ran for the first time in CI. They passed first time, which is luck as much as care. For changes like this, a pull request (which runs the scans but doesn't publish) is the safer first step.

---

## 8B.5 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A form returns `403` "This form has expired" | Missing or stale CSRF token (an old tab, a signed-out session) | Reload the page; for scripts, read `csrf_token` from the page first (`upload_many.py` does) |
| `403` on every POST behind a proxy | `Origin` doesn't match `PUBLIC_BASE_URL` (or the request's host) | Set `PUBLIC_BASE_URL` to the exact public `https://` address |
| Sign-in redirect blocked; console mentions `form-action` | The IdP's origin isn't in the CSP (its discovery failed when the policy was built) | Check the app can reach the discovery URL; it retries after 60 s |
| Styles missing; console shows CSP `style-src` violations | An inline `style=` or `<style>` came back into a template | Move it into `app.css`; `test_security.py` catches this in CI |
| `429` locally for everyone | All local traffic shares Docker's gateway address | Wait out the window (10 minutes for sign-in), or lower `LIMITS` in tests only |
| Rate limits never trigger on AWS | `TRUSTED_PROXY_HOPS` doesn't match the number of proxies | 2 behind CloudFront + ALB; 1 behind an ALB alone; 0 when reached directly |
| `curl` prints `000` | No connection: the service isn't up yet | `docker compose ps`; try again in a moment |
| `trivy config` fails in CI on a new finding | A HIGH or CRITICAL misconfiguration not listed in `.trivyignore.yaml` | Fix it, or add a scoped entry with a statement and expiry date |
| `trivy config` fails on an old, accepted finding | Its `expired_at` date has passed | Decide again: fix, or renew with a fresh reason |
| Image scan fails | A fixable CRITICAL CVE | Rebuild on an updated base or bump the package; the table shows the fixed version |
| `cosign verify`: `no matching CertificateIdentity` | Signed by a different workflow, branch or repository | That's the check working; deploy only images built by `main`'s CI |
| Deploy step fails in "Refuse an image this workflow didn't sign" | The sign job failed or was skipped, or ECR read permissions are missing | Check "Sign the image"; staging needs `ecr:ListImages`, production its `read-images` policy |
| `docker login` to ECR: `400 Bad Request` (PowerShell 5) | Piped text re-encoded | `cmd /c "aws ecr get-login-password … | docker login …"` |
| `The stub received bad data` | Windows credential store can't hold the ECR token | Pass the token to the tool directly (`cosign --registry-password`) |
| Staging returns `401` with the right password | The app started before the password changed in SSM | `--force-new-deployment`, wait for one `PRIMARY` deployment |

---

## 8B.6 Checklist

You've seen security in practice when you can:

- [ ] Run a ZAP baseline scan and explain each remaining warning
- [ ] Read the CSP header line by line and say what each directive blocks
- [ ] Show a cross-site POST refused, and explain the three CSRF layers
- [ ] Show that a forged `X-Forwarded-For` doesn't change who the app thinks the client is, and why counting from the right works
- [ ] Trip a rate limit, and explain why per-IP limits must be generous
- [ ] Show the infrastructure scan's accepted findings, with reasons and expiry dates
- [ ] Find a CVE in an image scan and decide what to do about it
- [ ] Download an SBOM and answer "do we ship package X?"
- [ ] Verify an image's signature for the right identity, and see it refused for the wrong one
- [ ] Explain why Trivy is installed from a checksummed release rather than through its GitHub Action
- [ ] Rotate a secret that was exposed, without displaying the new value

---

**Next:** Chapter 9, LLMs in production: choosing and routing models, prompts as code, structured output, evaluation, cost and latency, prompt injection, and keeping model inputs and outputs inside tenant boundaries.
