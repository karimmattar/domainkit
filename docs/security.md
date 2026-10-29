# Hardening a deployment

A checklist to go through before a service takes real users, and again
after each minor upgrade. Each item names the setting or command that does
it, and whether the deployment checks enforce it. Why each one matters, the
trust boundaries, and what remains a risk are in
[threat-model.md](threat-model.md); how to report a vulnerability in
domainkit itself is in [SECURITY.md](../SECURITY.md).

"Refused" means startup fails with `ENVIRONMENT` set to `staging` or
`production`, naming the setting; "warned" means `python manage.py check`
warns there ([reference/checks.md](reference/checks.md) has the whole
list). Run `check` with the production settings in CI, so neither
surprises a rollout.

## Before the first deployment

**Settings and secrets**

- [ ] `ENVIRONMENT = "production"`, so every check below applies. The
      settings `init` writes do this.
- [ ] `DEBUG` off, and no `DebugToolbarMiddleware` (refused).
- [ ] `SECRET_KEY` of 50 random characters or more, from a file
      (`<PREFIX>SECRET_KEY_FILE`), not the code (refused when short, weak,
      or a development key). Rotate it through `SECRET_KEY_FALLBACKS`.
- [ ] Every other secret from the environment or a file: database,
      broker, cache, and ledger credentials, `EVIDENCE.SUBJECT_KEY`, the JWT
      key. URLs that carry passwords are kept secret in logs and reprs.
      Replace the JWT key on a schedule through `AUTH.JWT.PREVIOUS_KEYS`,
      which keeps the old one for a token lifetime after it retires, then
      refuses its tokens; a key that leaked, without it
      ([auth.md](auth/authentication.md#replacing-a-key); a previous key
      that is the signing key, or retires more than a day ahead, is
      refused).
- [ ] `ALLOWED_HOSTS` names the service's hosts (a wildcard is refused).

**The edge**

- [ ] TLS terminated in front of the service, `SECURE_HSTS_SECONDS` set
      (warned when 0), and `TRUSTED_PROXIES` naming the proxies' networks
      only (every address is refused; none behind a load balancer is warned).
- [ ] The server run with its own limits and `--no-proxy-headers`
      ([reference/servers.md](reference/servers.md)), behind a proxy that
      bounds headers and slow clients.
- [ ] The proxy sets `X-Request-ID` itself, over a client's or in a line
      after it, or `ACCEPT_REQUEST_ID = "never"`: request ids come from trusted proxies
      only, so a caller's own never names log lines or audit rows
      ([operations.md](operations.md#request-ids)).
- [ ] `RATE_LIMIT` set (warned when missing), counted in Redis
      (`RATE_LIMIT_BACKEND = "domainkit.http.ratelimit.RedisBackend"`):
      with the auth app, attempts counted in each process are refused
      (`auth.E105`; `AUTH.REQUIRE_SHARED_THROTTLES = False` warns instead,
      for one process). Password and code checks stop while Redis is down
      (`AUTH.THROTTLE_FAIL_CLOSED`, on by default there): an alarm on
      `503 throttle_unavailable`
      ([runbook.md](runbook.md#logins-are-refused-while-redis-is-down)).
- [ ] `REQUEST_TIMEOUT` and `MAX_CONCURRENT_REQUESTS` with their middleware
      (warned when missing).
- [ ] `API_DOCS` off unless the API is public on purpose (warned).

**Data stores**

- [ ] Every network database with `SSLMODE = "verify-full"` (refused
      otherwise), no `ECHO` or `LOG_PARAMETERS` (refused).
- [ ] The application on a row-level role, and `migrate` on the schema
      owner (`MIGRATION_URL`).
- [ ] Redis over `rediss://` with a password or a client certificate
      (refused otherwise), and a broker over `amqps://` with a user of its
      own (refused otherwise).
- [ ] Backups encrypted, access to them controlled, and a restore tested
      ([operations.md](operations.md#backups-and-restores)).

**Who may do what**

- [ ] A rule on every route, and object rules where users share a table
      ([auth.md](auth/routes.md#protecting-routes)). `REQUIRE_ROUTE_RULES` (on by
      default) refuses a route without one.
- [ ] Write schemas that hold only the fields clients may set
      (`schema_for(..., write=True, include=[...])`).
- [ ] Staff hold only the permissions they use; the admin API mounted only
      where staff reach it.
- [ ] Two-factor logins for staff ([auth.md](auth/logging-in.md#two-factor-logins)),
      and session cookies with the `__Host-` prefix (warned otherwise).
- [ ] `LIVE_REAUTH_INTERVAL` set, so WebSockets and event streams drop a
      revoked token (warned when `None`).

**Evidence and personal data**

- [ ] The audit trail's four pieces: a ledger with TLS and the server's
      signing key (refused without them), `AUDIT.NAMESPACE`, sealing on a
      schedule, and the `protect()` triggers (warned, or refused with
      `AUDIT.REQUIRE_BASELINE`) ([operations.md](operations.md#sealing-the-audit-trail)).
- [ ] `auditverify` daily, with an alert when it fails.
- [ ] Personal data marked, and `privacy purge` on a schedule
      ([privacy.md](privacy.md)).
- [ ] `EVIDENCE.SUBJECT_KEY` set where evidence is kept (refused without).

**Outside calls**

- [ ] Outgoing calls to URLs someone else chose (webhooks, callbacks) go
      through a guarded client (`HttpClient(guard=True)`), which calls
      only public addresses ([runtime.md](runtime.md#outgoing-http-calls)).
- [ ] No loopback or link-local network in
      `WEBHOOKS.ALLOWED_PRIVATE_NETWORKS` (refused).

## While it runs

- [ ] Alerts on the metrics in [operations.md](operations.md#observability),
      each with its [runbook](runbook.md) entry.
- [ ] Patch releases taken as they come: they carry security fixes only
      for the latest minor ([versioning.md](versioning.md#security-fixes-and-support)).
- [ ] `pip-audit` (or the platform's scanner) over the deployed image's
      packages, on a schedule: new advisories appear with no change here.
- [ ] Operators named in the audit trail: commands that change data
      (`messages --retry`, `auditforget`, `flush`, `privacy purge`) take
      `--operator` or `$DOMAINKIT_OPERATOR`; `loaddata` and `flush`
      refuse without one (refused).
- [ ] `loaddata --include-guarded` and `flush --include-guarded` kept for
      a restore you planned: without it, users, permissions, grants,
      tokens, and second factors are left alone
      ([reference/commands.md](reference/commands.md#data-fixtures-flush-and-seeds)).

## Verifying a release

Each release on PyPI carries SLSA build provenance for the wheel and the
sdist, and a CycloneDX bill of materials attested for the wheel. Check a
file before deploying it:

```text
gh attestation verify domainkit-<version>-py3-none-any.whl --repo karimmattar/domainkit
```
