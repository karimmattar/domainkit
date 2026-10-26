# Threat model

**Draft (0.9.2).** This is a first version, written for the teams that
build services on domainkit and for their security reviewers. It says what
a domainkit service has to protect, where its trust boundaries are, what
can go wrong at each (by STRIDE), what domainkit does about it, and what is
left to the project. It describes the framework as released; it does not
replace a project's own threat model, which knows its data, its users, and
its deployment.

To report a vulnerability, follow [SECURITY.md](../SECURITY.md).

## What a service protects

| Asset | What can go wrong | Where domainkit holds it |
| --- | --- | --- |
| Money | An amount changes in transit or rounding; two concurrent changes overwrite each other; a retried request pays twice; a payment's follow-up work is lost or done twice. | Exact decimals end to end, written as fixed-point text ([databases.md](databases.md#exact-amounts)); optimistic versions on every aggregate ([databases.md](databases.md#versions)); row locks ([queries.md](queries.md)); idempotency keys ([contrib.md](contrib/idempotency.md#idempotency-safe-retries-of-unsafe-requests)); the outbox, committed with the change ([contrib.md](contrib/messaging.md#messaging-outbox-durable-handlers-background-tasks)). |
| Credentials | Passwords, tokens, second factors, signing keys, and database and broker passwords are guessed, stolen, or logged. | Password hashing and throttles, opaque tokens kept as digests, second factors encrypted at rest ([auth.md](auth.md)); one `SECRET_KEY`, with fallbacks for rotation, from which each purpose's key is derived ([reference.md](reference.md#signing)); secrets left out of reprs and logs. |
| The audit trail | Who did what is changed, deleted, or never written; an operator acts anonymously. | An append-only trail with a hash chain, sealed and anchored in a ledger ([contrib.md](contrib/audit.md#audit-who-did-what-when-from-where)); operators named and audited ([reference.md](reference.md#operators)); evidence in an immudb ledger, verified by the client ([contrib.md](contrib/evidence.md#evidence-records-that-provably-never-change)). |
| Personal data | A person's data is shown to someone else, kept longer than allowed, or cannot be exported or erased. | Deny-by-default route rules and object permissions ([auth.md](auth/routes.md#protecting-routes)); personal markers, pseudonyms in the trail, export, erasure, and retention ([privacy.md](privacy.md)); outbox messages blanked once sent ([mail.md](mail.md)). |

The service's availability matters to all four: a service that is down
cannot pay, and one that is overloaded may fail half-way.

## Trust boundaries

```text
        clients (browsers, apps, other services)
             |  (1) the internet
     load balancer / reverse proxy  (TLS ends here)
             |  (2) the edge: proxy headers, Host, scheme
   +-------- application processes (one event loop per core) --------+
   |   middleware -> authentication -> route rule -> use case -> UoW |
   +--+-----------+--------------+--------------+------------+-------+
      | (3)       | (4)          | (5)          | (6)        | (7)
   SQL database  cache/Redis   broker/outbox   immudb      outside services
                               and workers     ledger      (webhooks, HTTP calls)

   operators: manage.py commands, the admin API    (8)
   source, CI, and the package on PyPI             (9)
```

1. **Client to edge.** Anyone on the internet. Nothing the client sends is
   trusted: body, headers, path, tokens, `Idempotency-Key`, `X-Request-ID`.
2. **Edge to application.** The proxy is trusted only as far as
   `TRUSTED_PROXIES` says: its `X-Forwarded-For` and scheme header name
   the client, and its `X-Request-ID` the request.
3. **Application to the SQL database.** The database holds the aggregates,
   the outbox, the audit trail, users and tokens. The application's role
   should read and write rows; the schema owner migrates.
4. **Application to the cache.** Shared caches, rate-limit counts, and
   revocation stamps. Failing open is a design choice
   ([ADR 0012](adr/0012-caches-json-values-fail-open.md)).
5. **Application to the broker and the workers.** Work leaves the request
   through the outbox; workers run it later, possibly twice.
6. **Application to the ledger.** immudb keeps evidence and the trail's
   anchors; the client verifies what it reads.
7. **Application to outside services.** Outgoing webhooks and HTTP calls go
   to addresses a user may choose; inbound webhooks are signed.
8. **Operators.** People with a shell, the admin API, or the database.
9. **Supply chain.** domainkit's source, its CI, the package on PyPI, and
   the dependencies it resolves.

## STRIDE per boundary

Each table lists, per threat, what domainkit does and what remains. "The
project" is the team that builds and runs the service.

### 1-2. Clients and the edge

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Spoofing** a user | Opaque tokens, JWTs, cookie sessions with CSRF, client certificates ([auth.md](auth/authentication.md#authenticating-requests)); password and code throttles that count only failures ([auth.md](auth/logging-in.md#logging-in-and-out)); second factors ([auth.md](auth/logging-in.md#two-factor-logins)). | Throttles are shared and fail closed where deployed: counted in each process, they are refused (`auth.E105`), and while Redis cannot count, password and code checks are refused, so an outage locks users out rather than letting guesses through. JWTs name their key: a key is replaced on a schedule through `AUTH.JWT.PREVIOUS_KEYS`, whose tokens are refused one token lifetime after it retires; a key that leaked is replaced without it, which refuses its tokens at once ([auth.md](auth/authentication.md#replacing-a-key)). A stored per-account lockout is planned for 0.10. |
| **Spoofing** an address or scheme | Proxy headers believed from `TRUSTED_PROXIES` only, walked from the right ([ADR 0072](adr/0072-walk-proxy-headers-from-the-right-and-trust-unix-sockets.md)); `ALLOWED_HOSTS` checked first. | The project must list its proxies, and make the server read no proxy header itself ([reference.md](reference.md#http)). |
| **Tampering** with requests | Bodies validated by pydantic; `If-Match` on changes ([resources.md](resources.md)); signed values ([reference.md](reference.md#signing)). | Route schemas are the project's: a write schema should name the fields a client may set. |
| **Repudiation** | Request ids, from trusted proxies only ([ADR 0099](adr/0099-take-request-ids-from-trusted-proxies-only.md)), the access log, and audit rows with the actor and address ([contrib.md](contrib/audit.md#audit-who-did-what-when-from-where)); refused logins audited. | The proxy must set `X-Request-ID` over a client's, or `ACCEPT_REQUEST_ID = "never"` ([operations.md](operations.md#request-ids)). The project keeps logs and the trail as long as its regulator asks. |
| **Information disclosure** | Deny by default: a route says who may call it, or the service does not start ([auth.md](auth/routes.md#deny-by-default)); errors never echo input ([reference.md](reference.md#errors)); security headers, a Content-Security-Policy, and HSTS; API docs off when deployed, and loaded from the API's own origin; route templates, not paths, in logs; `/metrics` behind a token. | Object-level rules for multi-tenant data are the project's ([auth.md](auth/users.md#grants-on-single-objects)). |
| **Denial of service** | Request deadlines and load shedding ([runtime.md](runtime.md#request-deadlines), [runtime.md](runtime.md#load-shedding)); body size and time limits; rate limits ([cache.md](cache.md#shared-rate-limits)); bounded live connections ([live.md](live.md)); bounded hashing. | Volumetric attacks need the project's edge (CDN, WAF, provider limits). |
| **Elevation of privilege** | Model and object permissions; staff grant only what they hold; protected user fields ([auth.md](auth/routes.md#the-admin-api)); routes added late are checked at startup. | Permission design is the project's. |

### 3. The SQL database

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Spoofing** the server | `SSLMODE = "verify-full"` required for network databases where deployed ([reference.md](reference.md#deployment-checks)). | Certificates and their rotation. |
| **Tampering** with rows | Bound parameters only; optimistic versions refuse a lost update, and a table without `version` is refused ([databases.md](databases.md#versions)); `protect()` triggers keep the trail append-only ([contrib.md](contrib/audit.md#tamper-evidence)). | Who can connect as the schema owner; `protect()` must be migrated in (`check` warns without it). |
| **Repudiation** | The trail is written in the transaction of the change. | Changes made outside the application (a `psql` session) are not audited. |
| **Information disclosure** | Bound values never logged where deployed (`LOG_PARAMETERS` refused); TOTP secrets encrypted; tokens stored as digests; audit pseudonyms. | Encryption at rest, backups and their access, row-level security. |
| **Denial of service** | Statement, lock, idle, connect, and pool timeouts ([runtime.md](runtime.md#database-timeouts-and-pgbouncer)). | Pool sizing for the load ([performance.md](performance.md)). |
| **Elevation of privilege** | A row-level role for the application, a schema-owner URL for migrations only (`MIGRATION_URL`). | Granting those roles. |

### 4. The cache and rate-limit store

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Spoofing** / **Tampering** | `rediss://` with a password or client certificate required where deployed; values are JSON, never pickle ([cache.md](cache.md#values-are-json)). | Network access to Redis. |
| **Information disclosure** | Keys namespaced per service; principals cached in process, not in Redis ([ADR 0058](adr/0058-cache-principals-and-stamp-revocations.md)). | What the project caches. |
| **Denial of service** | A failing cache is skipped for a cooldown; rate limits let requests through while Redis fails, and password and code checks are refused instead in staging and production (`AUTH.THROTTLE_FAIL_CLOSED`) ([reference.md](reference.md#http)). | While Redis is down, nobody logs in ([runbook.md](runbook.md#logins-are-refused-while-redis-is-down)); the request limit still fails open. |

### 5. The broker, the outbox, and the workers

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Spoofing** / **Tampering** | `amqps://` and a user of its own required where deployed; the outbox row is the record, the broker only dispatches ([ADR 0014](adr/0014-outbox-as-record-broker-for-dispatch.md)). | Broker access control. |
| **Repudiation** | Operators who retry or cancel messages are named and audited. | |
| **Information disclosure** | Arguments blanked once a message is done, cancelled, or a sensitive task dies ([mail.md](mail.md)). | Arguments of tasks not marked `sensitive` stay in dead letters until retried. |
| **Denial of service** | Leases, retries with backoff, timeouts per task, a circuit breaker for the broker, bounded cleanup ([contrib.md](contrib/messaging.md#messaging-outbox-durable-handlers-background-tasks)). | Alerting on a growing outbox. |
| **Tampering** by running twice | Messages are delivered at least once. | Handlers must be idempotent: there is no inbox of processed ids yet (planned work, see below). |

### 6. The ledger

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Spoofing** / **Tampering** | TLS and the server's signing key required where deployed; every state checked against it ([databases.md](databases.md#ledgers-immudb)). | Running immudb, and keeping its signing key. |
| **Repudiation** | Evidence and audit anchors are write-once; anchors namespaced per project ([ADR 0069](adr/0069-namespace-audit-anchors-and-check-the-baseline.md)). | Verification by a third party needs the project's own process. |
| **Information disclosure** | Evidence subjects are keyed digests, not identities ([ADR 0018](adr/0018-evidence-subjects-are-internal-ids.md)). | |

### 7. Outside services

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Spoofing** | Outgoing webhooks are signed; inbound ones are verified against a timestamp ([contrib.md](contrib/webhooks.md#webhooks-signed-events-for-other-services)). | Sharing secrets with receivers. |
| **Information disclosure** / **Elevation** (server-side request forgery) | Webhooks and guarded HTTP calls go only to checked public addresses, after DNS ([ADR 0036](adr/0036-webhooks-through-the-outbox.md), [runtime.md](runtime.md#outgoing-http-calls)). | Calls made with another client are not guarded. |
| **Denial of service** | Answers bounded in size as they arrive; deadlines, retries, and a circuit breaker per host; DNS lookups shared fairly between owners. | One owner's share of lookups is per subscription until 0.10 gives subscriptions an owner. |

### 8. Operators

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Repudiation** | Data-changing commands run as `operator:<name>` and are audited ([ADR 0070](adr/0070-name-the-operator-of-data-changing-commands.md)); `loaddata` and `flush` record the tables they touched and the fixture's SHA-256, and need a name given on purpose where deployed; staff changes through the admin API are audited. | Other commands take the login name when none is given. Direct database access is not audited by domainkit. |
| **Tampering** | `migrate --downgrade` needs `--allow-data-loss` where deployed; `loaddata` only adds rows unless `--replace`, and neither it nor `flush` touches users, grants, tokens, or second factors without `--include-guarded` ([ADR 0100](adr/0100-guard-credentials-and-grants-from-loaddata-and-flush.md)); the trail's seals show rewritten rows (`auditverify`). | Who holds production access. |
| **Information disclosure** | Secrets hidden in `diffsettings`, reprs, and dumps. | Shell access sees the environment. |

### 9. The supply chain

| Threat | What domainkit does | What remains |
| --- | --- | --- |
| **Tampering** with the package | Releases built in CI from `main`, approved, and published with trusted publishing and attestations; actions pinned to commit SHAs ([SECURITY.md](../SECURITY.md)). | Verifying attestations when installing. |
| Vulnerable dependencies | pip-audit and a secret scan on every push; updates through Renovate. | The project's own lock file and its updates. |

## Mitigations at a glance

Everything startup refuses where deployed is listed in
[reference.md](reference.md#deployment-checks), with the weaker choices
`check` warns about. The production settings `init` writes start from the
safe side: docs off, HSTS on, a rate limit, and the deployment checks.

## Residual risks

What domainkit does not yet do, as of 0.9.2. Each is either planned or
left to the project on purpose.

- **Login throttles keep a known username out.** `LOGIN_RATE` wrong
  passwords a minute for a username hold its user back with 429, from any
  address; a stored per-account lockout is planned for 0.10.
- **Lookup threads per webhook subscription, not per tenant.** Whoever may
  create several subscriptions can hold the guard's lookup threads for one
  round of slow DNS answers, until 0.10 gives subscriptions an owner.
- **Cache computations are unbounded by default** (`COMPUTE_TIMEOUT =
  None`, as in 0.9.0); `check` warns where deployed, and 0.10 plans a
  bounded default.
- **Tamper evidence can be turned off.** Since 0.10 staging and production
  refuse to start without a ledger, a namespace, sealing, or the
  `protect()` triggers; `AUDIT.REQUIRE_BASELINE = False` turns that back
  into warnings, and so does `IDEMPOTENCY.REQUIRE_DEADLINE = False` for
  keyed requests without a deadline.
- **Concurrency under contention.** The default write path keeps its
  invariants with optimistic versions, but refuses most concurrent writers
  of one aggregate with 409, and there is no retry of a unit of work, no
  isolation setting, and no lock API beyond one row. Use `get_for_update`
  where writers contend ([queries.md](queries.md)).
- **Handlers run at least once.** A crash between a handler's commit and
  its message's mark runs it again; there is no inbox.
- **A committed idempotency key runs again once its tombstone expires.**
  Since 0.11 it is kept, without its response, for
  `IDEMPOTENCY.TOMBSTONE_SECONDS` (7 days) past its TTL (1 day); keep the
  two above the time clients retry, and refuse what must never happen
  twice with a unique column of the domain's own.
- **Open findings of the 0.9.0 audit.** [audit/0.9.0.md](audit/0.9.0.md)
  lists every one with its status; open security findings are listed by id
  and severity until they are fixed.

## What a project must do itself

- Terminate TLS, and set `TRUSTED_PROXIES` to its proxies only.
- Keep secrets out of the code: `SECRET_KEY`, database, broker, cache, and
  ledger credentials, `EVIDENCE.SUBJECT_KEY`, the JWT key, from the
  environment or files; rotate `SECRET_KEY` with `SECRET_KEY_FALLBACKS`.
- Give the application a row-level database role and migrate with the
  schema owner (`MIGRATION_URL`); migrate the audit trail's `protect()`
  triggers.
- Use Redis for `RATE_LIMIT_BACKEND` (staging and production refuse
  attempts counted in each process), and watch for the `503
  throttle_unavailable` answers that its outages cause.
- Replace the JWT key on a schedule through `AUTH.JWT.PREVIOUS_KEYS`,
  and remove the old key once its window ends; a key that leaked, at
  once, without listing it.
- Write a rule for every route, and object-level rules where users share
  tables; keep write schemas to the fields clients may set.
- Make durable handlers and outgoing calls idempotent; use
  `get_for_update` where writers contend.
- Mark personal data, and run retention (`privacy purge`) on a schedule.
- Run sealing on a schedule, anchor in a ledger, and run `auditverify`.
- Keep logs, the audit trail, and backups for as long as its regulator
  asks, encrypted, with access controlled and itself audited.
- Watch the metrics: the outbox's backlog, refused requests, shed load,
  and deadlines.
- Protect the edge from volumetric attacks, and test the service itself:
  this model covers the framework, not the project's code.
- Follow [docs/versioning.md](versioning.md#security-fixes-and-support):
  take patch releases as they come, and upgrade one minor at a time.
