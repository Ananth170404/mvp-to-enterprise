# Chapter 7: Auth, identity and multi-tenancy

> **In this chapter:** who is using the system, what they're allowed to do, and making sure one customer can never see another's data. Using an identity provider instead of storing passwords, the sign-in flow and what to verify in it, sessions, invitations and account linking, passwords, MFA and passkeys, enterprise single sign-on and provisioning, authorization models, multi-tenancy models, row-level security, machine access, and support access.
>
> **Part 2, [Auth and tenancy in practice](07b-auth-demos.md),** replaces Ledgerline's development login with OpenID Connect sign-in (Keycloak locally, Amazon Cognito on staging), and adds Postgres row-level security as a second wall between tenants, then proves it with a deliberately buggy query.

---

## 7.1 Why it matters

Every earlier chapter assumed we knew who the user was. Until now, Ledgerline didn't really: a development login let anyone pick any seeded user, and a shared site password kept strangers out of staging. That's fine for a demo and unacceptable for a product.

Identity and tenancy failures are different from other bugs:

- **They're silent.** A slow page gets complaints. A customer seeing another customer's invoices may never be reported, until it's in a breach notification.
- **They're often unrecoverable.** You can roll back a deploy; you can't un-leak data.
- **Enterprise customers check them first.** Single sign-on, MFA, roles, audit logs and tenant isolation are on every security questionnaire (Chapter 13). Not having them ends the sale.
- **Most of the hard parts are already solved.** Password storage, MFA, account recovery and federation are commodity services. The work that's genuinely yours is the authorization model and tenant isolation.

> **Buy identity, build authorization.** Let an identity provider answer "who is this?". Your own code and data answer "what may they do, and in which tenant?"

---

## 7.2 The words

| Term | Means |
|---|---|
| **Authentication** (authn) | Proving who someone is: password, passkey, a code, a corporate sign-in |
| **Authorization** (authz) | Deciding what they may do: this tenant, this role, this invoice |
| **Identity provider** (IdP) | The service that authenticates people: Cognito, Entra ID, Okta, Google, Keycloak |
| **OAuth 2.0** | A standard for an app to get *access tokens* to call APIs on someone's behalf |
| **OpenID Connect** (OIDC) | A layer on OAuth 2.0 for *sign-in*: the app gets an **ID token** saying who signed in |
| **SAML 2.0** | An older XML-based sign-in standard, still common for enterprise SSO |
| **ID token** | A signed JWT from the IdP: issuer, audience (your app), subject (a permanent user ID), expiry, and claims such as email |
| **Session** | The app's own memory that this browser is signed in, after authentication |
| **Tenant** | A customer organisation; its users and data are separate from every other tenant's |

---

## 7.3 Don't store passwords: use an identity provider

Building sign-in yourself means storing password hashes correctly, rate-limiting guesses, detecting breached passwords, MFA, recovery by email, lockouts, and keeping all of it secure forever. An identity provider does this, and usually federation with other providers too.

| Option | Examples | Good for | Watch for |
|---|---|---|---|
| Your cloud's user directory | Amazon Cognito, Microsoft Entra External ID, Google Identity Platform | Already on that cloud; Terraform-managed; cheap at small scale | Plainer sign-in pages and fewer conveniences than the specialists |
| A specialist service | Auth0, Clerk, WorkOS, Stytch, Descope | Polished sign-in, enterprise SSO and SCIM made easy, good developer experience | Price per user or per enterprise connection grows quickly; your users live with a vendor |
| Self-hosted open source | Keycloak, Zitadel, Ory, Authentik | Full control, no per-user price, runs anywhere (also locally, as Ledgerline does) | You run, patch and secure a critical service |

All speak OIDC, so the app's code barely depends on which one you choose. Ledgerline's `app/auth.py` works unchanged against Keycloak locally and Cognito on AWS; only four settings differ.

> **Ledgerline** uses Amazon Cognito on AWS. Its Essentials tier (the default for new user pools) includes managed sign-in pages and MFA, and the first 10,000 monthly active users signing in directly are free, a free tier that doesn't expire. Keycloak runs in Docker Compose as the local stand-in.

---

## 7.4 The sign-in flow

For a web app with a server, the right flow is the **authorization code flow with PKCE**:

```
browser ── GET /auth/login ──> app: make state, nonce and a PKCE verifier; remember them
   │
   └── redirect ──> IdP sign-in page  (password, MFA, recovery: all the IdP's job)
                      │
   ┌── redirect back: /auth/callback?code=…&state=… ┘
   │
app: 1. state matches the one this browser was given?       (stops login CSRF)
     2. swap the code for tokens, server to server, with the client secret and the PKCE verifier
     3. verify the ID token: signature (the IdP's published keys), issuer, audience = us,
        not expired, nonce = ours                            (stops forged and replayed tokens)
     4. map the token's subject to one of OUR users          (7.6)
     5. start a session                                      (7.5)
```

The details that matter:

- **Exact redirect URIs.** Register `https://app.example/auth/callback` exactly, never a wildcard. A loose redirect lets an attacker receive someone's code.
- **The code is exchanged server-side** with a client secret. Tokens never pass through the browser's address bar.
- **PKCE** proves the party redeeming the code is the one that asked for it. It's required for apps without a secret (mobile, single-page apps) and recommended for all.
- **Verify everything in the ID token,** with a maintained library. A JWT that's decoded but not verified proves nothing.
- **Use the provider's discovery document** (`/.well-known/openid-configuration`) for endpoints and keys, and let keys rotate.

**Don't put the ID token or access token in browser storage** as your session. For a server-rendered or backend-for-frontend app, the browser should hold only a session cookie (7.5).

---

## 7.5 Sessions after sign-in

Once the IdP has said who someone is, the app keeps its own session. Two designs:

| | Server-side sessions | Self-contained tokens (JWT in a cookie) |
|---|---|---|
| The cookie holds | A random ID; the session is a row in your database or cache | Signed claims; nothing stored server-side |
| Sign out, disable a user, "sign out everywhere" | Immediate: delete the rows | Not until the token expires, unless you add a denylist (a server-side store again) |
| Cost | One lookup per request | No lookup |

For most products, **server-side sessions** are the better default: revocation is the feature you'll need on the day an account is compromised. Ledgerline stores only the SHA-256 of the session token, so a copy of the sessions table can't be used to sign in.

**Cookie flags:** `HttpOnly` (JavaScript can't read it), `Secure` (HTTPS only), `SameSite=Lax` (not sent on cross-site POSTs, which also blocks the classic cross-site form attack on state-changing actions). Add CSRF tokens if you use `SameSite=None` or accept state-changing GETs (don't).

**Lifetimes:** an **idle timeout** (signed out after a period without activity: Ledgerline uses 2 hours) and an **absolute** one (sign in again after 12 hours, whatever happens). Enterprise customers will ask to configure these.

**Sign-out must reach the IdP too.** Ending only your own session leaves the IdP's: the next "Sign in" lets the person straight back in without a password, which matters on shared computers. Use the IdP's logout endpoint (RP-initiated logout), passing the ID token as `id_token_hint` where supported. Ledgerline's first version missed this; its test caught it (part 2).

---

## 7.6 Identity is not an account

Signing in proves who someone is. It doesn't make them a customer's user. Keep the two separate:

- **Accounts exist because someone invited them.** An admin of the tenant (or your onboarding flow) creates the user, with a role. A person who authenticates but has no account gets "you don't have an account; ask your administrator", not a fresh empty tenant.
- **Link by the IdP's permanent subject, not by email.** Emails change and get reused. Ledgerline matches a first sign-in by **verified** email to an invited user, then stores the subject (`issuer|sub`) and uses only that afterwards.
- **Never trust an unverified email.** Matching on an email the person merely typed into another provider is a classic account-takeover hole.
- **Relinking is an admin action.** If someone's identity is recreated (a new IdP, a deleted and re-created account), the old link must be removed deliberately and audited. Ledgerline has `python -m app.maintenance unlink-identity --email …`; part 2 shows why it was needed.
- **Deprovisioning matters as much as provisioning.** When someone leaves a customer, their access must end: disable the user, delete their sessions. With enterprise SSO, the customer's IdP and SCIM (7.8) do this for you.

---

## 7.7 Passwords, MFA and passkeys

If your IdP handles passwords, configure it well:

- **Length, not composition.** NIST's current guidance (SP 800-63B-4, 2025) requires a minimum of 15 characters when a password is the only authenticator, and forbids composition rules such as requiring a mixture of character types. Also: allow long passwords (64+), check new passwords against known-breached lists where the IdP can, and don't force periodic changes, only changes after evidence of compromise.
- **MFA:** authenticator apps (TOTP) are a big step up from passwords alone. SMS codes are the weakest option (SIM swaps); offer them only as a fallback, if at all.
- **Passkeys** (WebAuthn) are phishing-resistant: the credential is bound to your domain, so a look-alike site can't use it. Most IdPs now support them (Cognito's Essentials tier does). They're where sign-in is heading.
- **Recovery is the weakest link.** "Forgot password" by email is only as strong as the email account. Make MFA reset an admin-assisted, audited process.
- **Rate limits and lockouts** are the IdP's job; check they're on (Keycloak's brute-force protection is enabled in Ledgerline's realm).

> **Ledgerline** started with a 12-character, "must contain upper, lower and a number" policy in Cognito, the console's habit. Checking it against NIST for this chapter changed it to 15 characters and no composition rules. MFA is `OPTIONAL` in staging; production would make it required, or delegate it to customers' own IdPs.

---

## 7.8 Enterprise single sign-on and provisioning

Larger customers won't create passwords in your product. They expect:

- **SSO:** their employees sign in through the customer's own IdP (Entra ID, Okta, Google Workspace), over SAML or OIDC. Your IdP **federates**: it trusts the customer's IdP for users from that customer.
- **Home-realm discovery:** "you@bigcorp.com" is sent to BigCorp's IdP automatically, usually by email domain.
- **Enforced SSO:** once a tenant has SSO, password sign-in for that tenant's users is turned off.
- **Just-in-time (JIT) provisioning:** a user's first SSO sign-in creates their account, with a role from a group claim. Simple, but nothing removes the account when they leave.
- **SCIM** provisioning: the customer's IdP creates, updates and deactivates users in your app over a standard API. Deactivation in their IdP removes access in yours. Enterprise customers increasingly require it.
- **Per-tenant configuration:** each enterprise customer has its own IdP connection, certificates (which expire), and attribute mapping. Make it something their admin, or your support team, can set up and test without a deploy.

Federated users are often priced separately. Cognito, for example, includes only 50 SAML/OIDC-federated monthly active users free per account, then charges per user. Specialist providers often price per enterprise connection instead. Budget for it before your first enterprise deal (Chapter 12).

---

## 7.9 Authorization: what may they do?

Authentication says who; authorization decides what. Get these right:

- **Check on the server, for every action.** Hiding a button isn't authorization. Every route that reads or changes data checks the user's tenant and permissions.
- **Check objects, not just routes.** The most common API vulnerability is **broken object-level authorization**: `/invoices/123` works for anyone signed in, whoever owns invoice 123. It's first on the OWASP API Security Top 10. Every lookup by ID must also be scoped to the tenant (and, where it matters, the user).
- **Roles map to permissions, permissions to code.** Code checks `can("invoice.approve")`, not `role == "approver"`, so new roles don't mean code changes.
- **Separation of duties** where money moves: the person who uploads an invoice shouldn't be the only approver, and approving your own invoice may need a second person.
- **Audit decisions:** who did what, and also who was refused (7.13, Chapter 8).

Models, from simplest:

| Model | Decides by | Example | Tools |
|---|---|---|---|
| **RBAC** (roles) | The user's role in the tenant | "Approvers may approve invoices" | A role column and a permission table |
| **ABAC** (attributes) | Attributes of user, resource and context | "Approvers may approve invoices under ₹5 lakh in their own cost centre" | Policy engines: Open Policy Agent, Cedar (Amazon Verified Permissions) |
| **ReBAC** (relationships) | Relationships in a graph | "Anyone who can view the folder can view its documents" | Zanzibar-style systems: OpenFGA, SpiceDB |

Start with RBAC in your own tables. Move to a policy engine when rules start to depend on amounts, ownership and context, and when customers ask to define their own roles.

---

## 7.10 Multi-tenancy models

How tenants share infrastructure:

| Model | Data | Isolation | Cost and operations |
|---|---|---|---|
| **Pool** (shared) | All tenants in the same tables, separated by `tenant_id` | Logical: application code, plus database policies (7.11) | Cheapest; one schema to migrate; noisy neighbours possible |
| **Bridge** | A schema or database per tenant on shared servers | Stronger; per-tenant backup and restore | Migrations run per tenant; connection counts grow |
| **Silo** | A separate deployment per tenant | Strongest; can be in the customer's region or account | Most expensive; fleet management becomes the job |

Most B2B products start pooled and add silos for the few customers who require (and pay for) them: regulated industries, data-residency needs, very large tenants (Chapter 13).

In a pooled system, the **tenant context** travels with every unit of work: the request (from the session), the job message (Chapter 6: Ledgerline's jobs now carry `tenant_id`), the log line, the trace, the cache key, the storage path. Anything that does work without a tenant is either a system job, deliberately, or a bug.

---

## 7.11 Row-level security: a second wall

Application filters are the first wall: every query says `WHERE tenant_id = :current`. But one forgotten filter, one raw-SQL report, one injection, and it's gone. **Row-level security (RLS)** in Postgres is a second wall in the database itself:

```sql
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON invoices
  USING      (tenant_id = current_setting('app.tenant_id', true))   -- which rows are visible
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));  -- which rows may be written
```

The app sets the tenant at the start of every transaction (`SELECT set_config('app.tenant_id', :tenant, true)`). For the app's database user, every query on that table then sees only that tenant's rows, whether or not it has a `WHERE` clause.

What makes it work in practice:

- **Transaction-local settings** (`set_config(…, true)`), so a pooled connection never carries one request's tenant into the next. Ledgerline sets it in a SQLAlchemy `after_begin` hook from the session's tenant.
- **Fail closed:** with no tenant set, the policy compares with NULL and matches nothing. A forgotten context returns empty pages, not someone else's data.
- **The app's user mustn't bypass it.** Table owners and superusers bypass RLS by default, and so does any role with `BYPASSRLS`. The app connects as a separate, non-owner user (Chapter 2's least-privilege user, now doubly important). Use `FORCE ROW LEVEL SECURITY` if the app ever must connect as the owner.
- **Child tables** without a `tenant_id` can be covered through their parent (`EXISTS (SELECT 1 FROM invoices WHERE id = invoice_id)`), or given their own `tenant_id`.
- **Identity tables are the exception.** Users, sessions and tenants are read during sign-in, before the tenant is known. Ledgerline leaves them out of RLS and protects them in code.
- **System jobs work tenant by tenant** with the same restricted user (Ledgerline's stuck-job sweep), instead of with a superuser that sees everything.
- **Test it in CI,** as the app's user: a query without a filter, another tenant's row by ID, an update and a write into another tenant (Ledgerline's `scripts/rls_check.py`, on every push).

**Deploy it in two steps.** Code that doesn't set the tenant will see *nothing* once policies are on. So, expand and contract (Chapter 2): first release the code that sets the tenant context everywhere, then the migration that enables the policies. Ledgerline did both in one release on staging, which was paused; in production, that would have emptied every page until the new code was out.

The cost is small. The policy adds a condition the planner can use with the existing `tenant_id` indexes, and the setting is one statement per transaction.

Other databases have equivalents (SQL Server's row-level security, Oracle's Virtual Private Database). With document stores or object storage, the equivalent is per-tenant credentials or key prefixes enforced by IAM policies.

---

## 7.12 Machine access: API keys and service identities

Not every caller is a person:

- **Customers' integrations** (Chapter 11) need API access. Issue **API keys** per tenant, each with a scope ("read invoices"), an owner, a creation date and a last-used date. Store only a hash, show the key once, give it a recognisable prefix (`ll_live_…`) so leaked keys can be found by secret scanners, and let customers rotate and revoke them. Or use OAuth's **client credentials** flow, which gives short-lived tokens instead of long-lived keys.
- **Your own services** authenticate to each other and to the cloud with workload identities (IAM roles for ECS tasks, workload identity federation), never shared static keys. Ledgerline's app and worker have separate roles with separate permissions (Chapter 6).
- **Webhooks you send** are signed so receivers can verify them; **webhooks you receive** are verified (Chapter 6).
- **Machine tokens carry a tenant too,** and go through the same RLS context as people.

---

## 7.13 Support access and impersonation

Your support team will need to see what a customer sees. Make it a feature, not a back door:

- **Explicit and time-limited:** a support engineer requests access to a tenant for a reason, and it expires (hours, not forever).
- **Visible to the customer:** an audit entry they can see, and for some customers, their approval first.
- **"View as" rather than "become":** read-only by default; actions taken during impersonation are recorded as the support engineer, on behalf of the user.
- **Break-glass** access to production data for incidents: rare, logged, alerted on, reviewed afterwards (Chapter 8).

---

## 7.14 By stage

| Stage | Sign-in | Authorization | Tenancy |
|---|---|---|---|
| MVP | A hosted IdP from the start, even for a pilot; or a dev-only login that can't run in production (Ledgerline's) | Two or three roles in your own tables; checks on the server | `tenant_id` on every row; every query filtered |
| Early production | OIDC with server-side sessions; MFA available; invitations; sign-out at the IdP | Object-level checks everywhere; audit of permission changes | RLS as a second wall, tested in CI; tenant in logs, traces, jobs and cache keys |
| Growth | Enterprise SSO (SAML/OIDC) per tenant; SCIM; passkeys; enforced MFA | Permissions instead of role checks; customer-defined roles; maybe a policy engine | Noisy-neighbour limits; per-tenant export and deletion; silos for the few who need them |
| Enterprise | Configurable session policies; IdP-initiated sign-in; certificate rotation tooling | Separation of duties; access reviews; least-privilege support access | Data residency; per-tenant encryption keys; contractual isolation evidence |

---

## 7.15 By product type

- **B2B SaaS:** invitations, roles per tenant, SSO and SCIM for larger customers, and RLS or stronger isolation. Users may belong to several tenants (accountants, agencies): design the user-to-tenant relationship as many-to-many early if that's likely.
- **AI and agentic products:** the agent acts *as someone*. It should carry that user's tenant and permissions into every tool call and retrieval, never a broader service identity. Retrieval (RAG) must filter by tenant before the model sees anything (Chapter 9); a vector search across all tenants' documents is a cross-tenant leak waiting for the right question. Actions an agent takes need the same authorization checks as a person's, plus approvals for risky ones (Chapter 10).
- **Internal tools:** sign-in through the company's IdP only; roles from its groups; nothing local.
- **Consumer products:** self-service sign-up, social sign-in, passkeys, strong bot and abuse protection; tenants are often just individual users or households.

---

## 7.16 Challenges and watch-outs

- **Home-made password storage** or sign-in flows.
- **ID tokens decoded but not verified,** or verified without checking audience and issuer.
- **Linking accounts by unverified email.**
- **JWTs as sessions with no way to revoke them.**
- **Sign-out that ends only your session,** leaving the IdP's.
- **Open redirects** after sign-in (`?next=https://evil.example`): accept only local paths.
- **Broken object-level authorization:** routes that check sign-in but not ownership.
- **Tenant filters only in code,** with nothing in the database behind them.
- **The app connecting as the table owner,** silently bypassing RLS.
- **Enabling RLS before the code sets tenant context,** and emptying every page.
- **A cache, a search index or a vector store without the tenant in its keys or filters.**
- **Password rules from the last decade:** composition rules and forced rotation.
- **Support access that nobody audits.**
- **Signals to act on:** a security questionnaire asking for SSO, SCIM or MFA enforcement; a customer with more than one subsidiary (tenants within tenants); the first request to "see what the customer sees"; any query or report that touches several tenants' data.

---

## 7.17 Exercise

For each situation, what's the risk, and what would you do?

1. A developer proposes storing the user's ID token in `localStorage` and sending it with every API call, "so we don't need a sessions table".
2. Your sign-in links accounts by email. A customer's employee signs in with a social provider that doesn't verify emails, using a colleague's address.
3. A new reporting endpoint uses raw SQL for speed, and forgets the tenant filter.
4. An enterprise customer's IT team asks how a departing employee loses access.
5. Your AI assistant answers questions using a vector search over all uploaded documents.
6. Support wants a "log in as customer" button.

<details>
<summary>Suggested answers</summary>

1. **Risk:** any script on the page (an XSS bug, a compromised dependency) can read and exfiltrate the token, and you can't revoke it before it expires. **Do:** keep tokens server-side; the browser gets an `HttpOnly` session cookie; sessions are rows you can delete.
2. **Risk:** account takeover, the attacker becomes the colleague. **Do:** link only on a *verified* email, once, then by the IdP's subject; don't accept providers that don't verify emails for linking; review who's linked to what.
3. **Risk:** one customer's report shows everyone's data. **Do:** with RLS, the database returns only the current tenant's rows anyway, which is why the second wall exists; still fix the query, and add the endpoint to cross-tenant tests.
4. **Answer:** with SSO, disabling them in the customer's IdP stops new sign-ins; with SCIM, it also deactivates their account and ends sessions. Without SCIM, sessions live until they expire, so keep idle timeouts short and offer admins "remove user", which deletes sessions immediately.
5. **Risk:** the right question retrieves another tenant's documents into the answer. **Do:** filter retrieval by tenant (and by the user's permissions) in the query itself, not after; test it like `rls_check` does.
6. **Do:** build "view as" with an expiring, reasoned, audited grant, visible to the customer; read-only by default; actions recorded as the support engineer. **Avoid:** a shared admin password or a session-cookie swap.

</details>

---

## 7.18 Cost

November 2026 list prices, US regions.

| Item | Approx. monthly | Notes |
|---|---|---|
| Amazon Cognito, Essentials tier | Free up to 10,000 monthly active users signing in directly; then $0.015 per user | The free tier doesn't expire. Lite is cheaper with fewer features; Plus adds threat protection with no free tier |
| Federated users (SAML or OIDC, enterprise SSO) | Cognito: 50 free, then $0.015 per user | Specialist providers often price per enterprise connection instead |
| Emails and SMS from the IdP | Per message (SES and SNS on AWS) | SMS MFA is the expensive and least secure option |
| Self-hosted Keycloak | A small container and a database | Plus your time to run, patch and back it up |
| Row-level security | Nothing | One setting per transaction; negligible query cost with tenant indexes |

The expensive parts aren't on the bill: a cross-tenant leak, or an enterprise deal lost for lack of SSO.

---

## 7.19 Demo

[Part 2, Auth and tenancy in practice](07b-auth-demos.md):

1. **Real sign-in, locally and on staging:** OIDC with Keycloak in Docker Compose, then Amazon Cognito on AWS. Server-side sessions, sign-out at the provider, a person who isn't invited, and a forged callback.
2. **Row-level security:** policies on every tenant table, the tenant set per transaction, a check that runs in CI, and a deliberately broken route that the database stops anyway.

---

## 7.20 Checklist

Your identity and tenancy are in good shape when:

- [ ] No passwords are stored by your application; an identity provider authenticates people
- [ ] Sign-in uses the authorization code flow with PKCE, exact redirect URIs and full ID-token verification
- [ ] The browser holds only an `HttpOnly`, `Secure`, `SameSite` session cookie; sessions are revocable
- [ ] Sessions have idle and absolute timeouts, and sign-out ends the IdP's session too
- [ ] Accounts exist only by invitation (or SSO provisioning), linked to the IdP's permanent subject
- [ ] Emails are trusted only when verified
- [ ] Password policy follows current guidance (length, breached-password checks, no composition rules or forced rotation); MFA is available, and required where it matters
- [ ] Enterprise customers can use their own IdP, and deprovisioning removes access
- [ ] Every read and write checks tenant and permission on the server, including lookups by ID
- [ ] Tenant context travels with requests, jobs, logs, traces and cache keys
- [ ] The database enforces tenant isolation (RLS or equivalent) for the app's user, tested in CI
- [ ] API keys are hashed, scoped, prefixed, rotatable and tied to a tenant
- [ ] Support access to customer data is time-limited, reasoned and audited

---

**Next:** Chapter 8, Security: threat modelling, the OWASP risks for web apps and APIs, secrets management, dependency and supply-chain security, encryption, audit logging, and responding when something goes wrong.

Sources: [Amazon Cognito pricing](https://aws.amazon.com/cognito/pricing/) · [NIST SP 800-63B-4, authenticators](https://pages.nist.gov/800-63-4/sp800-63b/authenticators) · [OWASP API Security Top 10](https://owasp.org/API-Security/) · [PostgreSQL: row security policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
