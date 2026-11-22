# Local identity provider (Keycloak)

`docker compose up` starts Keycloak with this realm imported, so local sign-in
works like staging's Amazon Cognito: the app redirects to a sign-in page,
the provider checks the password, and the app verifies the ID token it gets
back. Guide Chapter 7.

| Sign in as | Password | Is |
|---|---|---|
| priya@acme.example | priya-local-pw | Acme clerk |
| arjun@acme.example | arjun-local-pw | Acme approver |
| meera@globex.example | meera-local-pw | Globex approver |
| outsider@example.com | outsider-local-pw | known to Keycloak, not invited to Ledgerline |

These passwords and the client secret exist only for local development; they
are in the repository on purpose and must never be used anywhere else.

The users have fixed IDs, so the identity-provider subject each one is linked
to in Ledgerline (`users.oidc_subject`) stays the same when Keycloak is
recreated. Without that, rebuilding Keycloak would lock everyone out with
"This email is linked to a different sign-in" (guide 7B), the same thing that
happens when a real company moves to a new identity provider. The fix there is
`python -m app.maintenance unlink-identity --email …`.

Keycloak's admin console: http://localhost:8180/admin (user `admin`, password
`admin`, local only). The realm is re-imported only when Keycloak's data is
empty: after changing this file, `docker compose down -v` or delete the realm.
