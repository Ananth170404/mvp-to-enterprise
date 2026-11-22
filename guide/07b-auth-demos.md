# Chapter 7, part 2: Auth and tenancy in practice

> **In this part:** Ledgerline's development login gives way to real sign-in with OpenID Connect: Keycloak in Docker Compose locally, Amazon Cognito on staging, the same app code for both. Then Postgres row-level security becomes a second wall between tenants, proven by a check that runs in CI and by a route with its tenant filter deliberately deleted. For each: the steps, the real results, and what went wrong. Concepts are in [part 1](07-auth-identity-multitenancy.md).

Run in November 2026: locally on a Windows laptop (PowerShell 5, Docker Desktop) and in a Linux sandbox with a real browser (Playwright), and on AWS in `ap-southeast-2` (Sydney).

---

## Contents

- [7B.1 What changed in the code](#7b1-what-changed-in-the-code)
- [7B.2 Demo 1: sign-in and row-level security, locally](#7b2-demo-1-sign-in-and-row-level-security-locally)
- [7B.3 Demo 2: Amazon Cognito on staging](#7b3-demo-2-amazon-cognito-on-staging)
- [7B.4 What went wrong](#7b4-what-went-wrong)
- [7B.5 Troubleshooting](#7b5-troubleshooting)
- [7B.6 Checklist](#7b6-checklist)

---

## 7B.1 What changed in the code

| File | What it does |
|---|---|
| [`app/auth.py`](../ledgerline/app/auth.py) | OpenID Connect, authorization code flow with PKCE: `begin_login` (state, nonce, PKCE verifier), `finish_login` (state checked against the browser's cookie; code exchanged server-side; ID token verified for signature, issuer, audience, expiry and nonce; user found by subject, or on first sign-in by **verified** email among **invited** users), `create_session`, `end_session`, `logout_url` (with `id_token_hint`). `current_user` reads the session, enforces idle and absolute timeouts, and sets the tenant for row-level security. |
| [`app/main.py`](../ledgerline/app/main.py) | `/auth/login`, `/auth/callback`, `/logout`; the development login now creates the same kind of session; sign-in errors shown on the sign-in page; `?next=` limited to local paths. |
| [`app/models.py`](../ledgerline/app/models.py), migration [`0004`](../ledgerline/migrations/versions/0004_sessions_and_sign_in.py) | `users.oidc_subject` and `last_login_at`; `sessions` (only the SHA-256 of the cookie's token, the expiry, last activity, the ID token for sign-out); `login_requests` (sign-ins in progress). |
| [`app/db.py`](../ledgerline/app/db.py) | `set_tenant(session, tenant_id)`, applied to every transaction by an `after_begin` hook as `set_config('app.tenant_id', …, true)`. |
| Migration [`0005`](../ledgerline/migrations/versions/0005_row_level_security.py) | Row-level security on `documents`, `invoices`, `extraction_runs`, `purchase_orders`, `vendors`, `audit_events` (by `tenant_id`), and on `invoice_lines`, `po_lines` (through their parent). Identity tables (`tenants`, `users`, `sessions`, `login_requests`) are left out: they're read during sign-in, before the tenant is known. |
| [`app/jobs.py`](../ledgerline/app/jobs.py), [`app/extraction.py`](../ledgerline/app/extraction.py), [`app/maintenance.py`](../ledgerline/app/maintenance.py), [`app/drill.py`](../ledgerline/app/drill.py) | Work done outside a request carries its tenant: job messages include `tenant_id`; the stuck-job sweep goes tenant by tenant; the restore drill scopes its connections. `maintenance unlink-identity` forgets an identity link (7B.4). |
| [`scripts/rls_check.py`](../ledgerline/scripts/rls_check.py) | As the app's own database user: an unfiltered query, no tenant at all, another tenant's invoice by ID, changing it, moving a row into another tenant, another tenant's invoice lines. Exits 1 on any leak. CI runs it on every push. |
| [`deploy/keycloak/`](../ledgerline/deploy/keycloak/) | The local realm: the app's client (exact redirect URIs for ports 8000 and 8007, PKCE required), four users with fixed IDs (one of them not invited to Ledgerline), brute-force protection. |
| [`docker-compose.yml`](../ledgerline/docker-compose.yml) | Adds `keycloak` (26.4.5, `start-dev --import-realm`). The browser uses `localhost:8180`; the app reaches it as `keycloak:8180`, and `KC_HOSTNAME_BACKCHANNEL_DYNAMIC` makes Keycloak give it server-to-server URLs for the name it used. |
| [`infra/modules/environment/cognito.tf`](../ledgerline/infra/modules/environment/cognito.tf) | A Cognito user pool (Essentials tier, admin-created users only, email as username, optional TOTP MFA, a 15-character password minimum without composition rules), a managed login domain (version 2) and its style, the web client (code flow, secret, exact callback and logout URLs, `prevent_user_existence_errors`), and the client secret in SSM for ECS to inject. |
| [`tests/test_auth.py`](../ledgerline/tests/test_auth.py) | 13 tests against a fake provider with a real RSA key: linking, a changed email, uninvited and unverified users, a token signed by another key, for another app, from another issuer, expired, with a replayed nonce; login CSRF and replay; sign-out at the provider; idle and absolute expiry; safe `next`. |

---

## 7B.2 Demo 1: sign-in and row-level security, locally

**Cost:** free. About 20 minutes.

### Steps

From the `ledgerline` folder:

```powershell
# 0. (Once) keep your own port without editing docker-compose.yml:
#    docker-compose.override.yml, which Compose reads automatically, containing:
#      services:
#        api:
#          ports: !override
#            - "8007:8000"

# 1. Start everything, now including Keycloak (about 30 s to start the first time)
docker compose up -d --build

# 2. In the browser: http://localhost:8007/invoices
#    -> Sign in -> Keycloak (localhost:8180) -> priya@acme.example / priya-local-pw
#    -> Sign out -> Sign in again (it must ask for the password again)
#    -> sign in as outsider@example.com / outsider-local-pw (known to Keycloak, not invited)

# 3. The database's own check
docker compose run --rm api python -m scripts.rls_check

# 4. A deliberate bug: as Priya upload an invoice and copy its URL; as Meera
#    (meera@globex.example / meera-local-pw, in a private window) open it: 404.
#    Now delete ", Invoice.tenant_id == user.tenant_id" from get_invoice in
#    app/main.py, save (the API reloads), refresh Meera's window: still 404.
#    Undo the edit.
```

### Results

**Sign-in,** driven in a real browser:

```
1. priya: /invoices redirected to /login?next=%2Finvoices
   on provider page: localhost:8180/realms/ledgerline/protocol/openid-connect/auth
   back at /invoices after 2137 ms
   header shows: Priya (AP clerk) · clerk  Sign out
   session cookie: httpOnly true, sameSite Lax, 43 characters (random; only its hash is stored)
   after sign out: localhost:8000/login;  /invoices after sign out -> /login
2. sign in again goes to the password form: true
3. outsider: "You've signed in, but you don't have a Ledgerline account yet.
   Ask an administrator at your company to invite you."  has session cookie: false
4. wrong password: stays on the provider's page
5. forged callback (a state this browser wasn't given): 400
   "This sign-in link has expired or was started in another browser."
```

**`rls_check`,** on the laptop:

```
PASS  app connects as a user without BYPASSRLS  ledgerline_app, bypassrls=False
PASS  unfiltered query, as tenant A             1 invoice(s)
PASS  no tenant set                             0 invoices
PASS  tenant B's invoice by ID, as tenant A     not found
PASS  change tenant B's invoice, as tenant A    0 row(s) changed
PASS  move tenant A's invoice into tenant B     refused
PASS  tenant B's invoice lines, as tenant A     0 line(s)
PASS  for contrast: the schema owner sees both  2 invoices
Tenant isolation holds in the database.
```

The same check **with the policies removed** (migration 0005 downgraded) fails six of eight, which is what proves the check itself works:

```
FAIL  unfiltered query, as tenant A             2 invoice(s)
FAIL  no tenant set                             2 invoices
FAIL  tenant B's invoice by ID, as tenant A     found
FAIL  change tenant B's invoice, as tenant A    1 row(s) changed
FAIL  move tenant A's invoice into tenant B     ALLOWED
FAIL  tenant B's invoice lines, as tenant A     1 line(s)
```

**The deliberately broken route.** Meera (Globex) opening Priya's (Acme) invoice:

| Code | Database user | Result |
|---|---|---|
| Real code (filters by tenant) | `ledgerline_app` | 404 |
| Tenant filter deleted | `ledgerline_app` (RLS applies) | **404**: the database returned no row |
| Tenant filter deleted | `ledgerline` (table owner, bypasses RLS) | **200**: Acme's invoice, shown to Globex |

The last row is the leak the second wall exists for, and also the reason the app must never connect as the table owner.

Background work keeps working under RLS: an upload's extraction (the job carries its tenant), the stuck-job sweep (tenant by tenant, as the app's user) and the restore drill all ran as `ledgerline_app`, and sign-ins appear in the audit trail as `user / signed_in`.

---

## 7B.3 Demo 2: Amazon Cognito on staging

**Cost:** nothing beyond running staging; two users are far inside Cognito's free tier. About 45 minutes.

The order matters again: **Terraform first, then push.** Two other things change on staging. The development login is now off, so the Chapter 6 `upload_many` script no longer works there. And the new code turns on row-level security, which old code doesn't support; see 7B.4.

```powershell
# 1. Sign in; start the database
aws login --profile ledgerline
$Env:AWS_PROFILE = "ledgerline"
aws rds start-db-instance --db-instance-identifier ledgerline-staging --query "DBInstance.DBInstanceStatus" --output text
aws rds wait db-instance-available --db-instance-identifier ledgerline-staging

# 2. Commit everything (check git status), don't push yet
# 3. Cognito
cd infra\staging
terraform plan  -var alarms_enabled=false
terraform apply -var alarms_enabled=false
cd ..\..

# 4. Push; wait for deploy-staging (migrations 0004 and 0005, then app and worker)
git push

# 5. Invite people: in this pool only an admin can create accounts
$pool = terraform -chdir=infra/staging output -raw cognito_user_pool_id
$temp = "<a temporary password, 15+ characters>"
foreach ($email in "priya@acme.example", "outsider@example.com") {
  aws cognito-idp admin-create-user --user-pool-id $pool --username $email `
    --user-attributes Name=email,Value=$email Name=email_verified,Value=true `
    --temporary-password $temp --message-action SUPPRESS --query "User.UserStatus" --output text
}

# 6. Sign in at the staging URL: site password first (the staging gate), then Sign in
#    -> Cognito's page -> temporary password -> choose a permanent one -> back as Priya
# 7. Sign out, sign in again; sign in as the outsider
# 8. Pause staging
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 0 --query "service.desiredCount"
aws rds stop-db-instance --db-instance-identifier ledgerline-staging --query "DBInstance.DBInstanceStatus" --output text
```

`--message-action SUPPRESS` stops Cognito from sending an invitation email: the demo addresses don't exist. With real users, leave it out and Cognito emails the temporary password.

### Results

- **Terraform:** `Apply complete! Resources: 7 added, 1 changed, 1 destroyed`. The seven were the user pool, its domain (`ledgerline-staging-8b71fd.auth.ap-southeast-2.amazoncognito.com`), the web client, the managed login style, a random suffix and the client secret in SSM; the change was the task execution role's permission to read that secret. The "destroyed" one was the app's task definition, replaced with the new settings: `OIDC_DISCOVERY_URL`, `OIDC_CLIENT_ID`, `OIDC_LOGOUT_URL`, `PUBLIC_BASE_URL`, the secret, and `DEV_LOGIN=false`. Nothing in the account's restrictions blocked Cognito.
- **Users:** both created as `FORCE_CHANGE_PASSWORD`.
- **Deploy:** the app ran task definition revision 17, `1 1` desired and running.
- **Sign-in:** the site password, then Ledgerline's sign-in page (no development list now), then Cognito's page, a forced change of the temporary password, and back into Ledgerline as Priya. Sign-out went through Cognito's logout endpoint back to Ledgerline's sign-in page, and signing in again asked for the password.
- **The outsider** authenticated with Cognito and got "you don't have a Ledgerline account yet": Cognito knew who she was, but Ledgerline had never invited her.

---

## 7B.4 What went wrong

> **Keycloak asked the first user to "update" their account.** The local realm's users had last names like `(Acme clerk)`. Keycloak's user-profile rules reject parentheses in names, so after a correct password it showed an "Update account information" page instead of returning to Ledgerline, and the browser test timed out. Plain names (`Acme`) fixed it. Identity providers have their own validation and "required actions" (verify email, update profile, set up MFA, accept terms); any of them can appear between the password and your callback.

> **Signing out didn't sign out.** The first version deleted Ledgerline's session and sent the browser to Keycloak's logout endpoint with only `client_id`. Keycloak showed "Do you want to log out?" and, unconfirmed, kept its own session, so the next "Sign in" went straight through without a password. Fix: keep the ID token with the session and send it as `id_token_hint`, which lets the provider end its session without asking. The browser test that found this ("sign in again goes to the password form") stays.

> **Recreating Keycloak locked Priya out.** After Keycloak's container was rebuilt, signing in as Priya returned 403: "This email is linked to a different sign-in." Keycloak had created her again with a new random ID, so her token had a new subject, and Ledgerline, which had linked her account to the old subject, refused it. That's the correct, safe behaviour: an email match must not silently take over an account that's linked to another identity. Two fixes:
> - the local realm now gives its users **fixed IDs**, so rebuilding Keycloak doesn't change who they are;
> - `python -m app.maintenance unlink-identity --email …` removes a link deliberately, ends the person's sessions and records it in the audit trail. In production, that's the runbook step for moving to a new identity provider, or for an account recreated at the provider.

> **A 503 right after `git push`, again.** The staging URL was opened while deploy-staging was still running, and the app service had been at 0 since staging was paused (Chapter 6 had the same moment). Wait for the deploy to go green, then `1 1` running, then about a minute for the load-balancer health check.

> **The password policy was a habit, not a decision.** The first Cognito configuration required 12 characters with upper case, lower case and a digit, the common console default. Checking it against NIST SP 800-63B-4 for this chapter changed it to 15 characters and no composition rules (part 1, 7.7). The new policy applies to passwords set from then on.

> **Row-level security and the code that supports it shipped together.** On staging that was safe: the app was at 0 tasks while the migrations ran. In production it would not be: between migration 0005 and the new app tasks starting, the old code, which never sets the tenant, would have seen no rows at all, so every page empty and every extraction "not found". The safe order is two releases: first the code that sets the tenant context (it works with or without policies), then the policies.

> **A test that counted database calls broke.** Signing a test user in now creates a session row, so a tracing test that cleared its recorded spans before signing in saw the sign-in's queries as well. Signing in first, then clearing, fixed it. Small, but typical: moving sign-in from "a cookie with a user ID" to real sessions touches every test that pretends to be someone.

---

## 7B.5 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| After the password, Keycloak shows "Update account information" | A required action, e.g. names that fail Keycloak's user-profile validation | Fix the user's attributes in the realm file or admin console |
| `This email is linked to a different sign-in` | The person's identity at the provider changed (a rebuilt Keycloak, a new IdP) | `python -m app.maintenance unlink-identity --email …`, then sign in again |
| `You don't have a Ledgerline account yet` | Authenticated, but no invited user with that verified email | Create (invite) the user in Ledgerline first |
| `This sign-in link has expired or was started in another browser` | The state cookie is missing or different: a different browser, an expired 10-minute window, or a reused link | Start again from Sign in |
| Sign-out, then Sign in goes straight back in | The provider's session wasn't ended | Check `OIDC_LOGOUT_URL` (Cognito) or that the provider receives `id_token_hint` |
| Keycloak error `Invalid parameter: redirect_uri` | The app's callback URL isn't registered in the client | Add it exactly (scheme, host, port, path) |
| Cognito `redirect_mismatch` | `PUBLIC_BASE_URL` doesn't match the client's callback URL | Both come from Terraform; re-apply and redeploy |
| App won't start: `Deployed sign-in requires PUBLIC_BASE_URL` | OIDC configured without the public `https://` address | Set it (Terraform does, from the CloudFront domain) |
| Pages empty / jobs "not found" after migration 0005 | Code that doesn't set the tenant, or the app connected without `set_tenant` | Deploy the code that sets tenant context first; check `current_user` ran |
| `rls_check`: `bypassrls=True`, or the owner check fails | The app connects as the table owner or a superuser | Use the restricted app user (`python -m app.db_roles`) |
| `new row violates row-level security policy` | Writing a row for a tenant other than the transaction's | That's the policy working; find the code path that mixed tenants |
| 503 from the staging URL | The app service is at 0 or not yet healthy | Wait for deploy-staging, then `1 1` running, then a minute |

---

## 7B.6 Checklist

You've seen auth and tenancy in practice when you can:

- [ ] Sign in through an identity provider and explain each check in the callback
- [ ] Show the session cookie's flags, and that only its hash is stored
- [ ] Sign out and show the provider's session ended too
- [ ] Show an authenticated but uninvited person getting no account
- [ ] Explain why accounts link by subject, not email, and how to relink safely
- [ ] Run `rls_check` and explain each line, with and without the policies
- [ ] Show a route with its tenant filter removed still unable to leak, and why the table owner could
- [ ] Explain why row-level security and the code that sets the tenant ship as two releases
- [ ] Create users in Cognito and sign in to staging through it

---

**Next:** Chapter 8, Security: threat modelling, the OWASP risks for web apps and APIs, secrets management, dependency and supply-chain security, encryption, audit logging, and responding when something goes wrong.
