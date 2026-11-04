# Security

This page is the hardening checklist for a domainkit service. Go through
it before a service takes real users, and again after each minor
upgrade. Each item names the setting or command that does it, and says
whether the deployment checks enforce it. Why each item matters, the
trust boundaries, and what remains a risk are on
[threat-model.md](threat-model.md). How to report a vulnerability in
domainkit itself is on [SECURITY.md](../SECURITY.md).

On this page you learn how to:

- run the deployment checks, and read what they refuse and what they
  warn about;
- harden the settings, the edge, the data stores, and who may do what;
- keep evidence: the audit trail, operators, and personal data;
- guard outgoing calls;
- verify a domainkit release before you deploy it.

## How the checks work

With `ENVIRONMENT` set to `staging` or `production`, domainkit runs the
deployment checks ([the glossary](learning.md#words)). Each check has an
id, such as `security.E001`:

- **Refused** (an `E` id) means that startup, and every command, fails
  and names the setting. Errors cannot be silenced.
- **Warned** (a `W` id) means that `python manage.py check` warns there.
  `check --deploy` fails on each warning that `SILENCED_CHECKS` does not
  list.

[reference/checks.md](reference/checks.md) has the whole list. Run
`check --deploy` with the production settings in CI, so that neither
kind surprises a rollout.

A settings module with `DEBUG = True` and `ENVIRONMENT = "production"`
does not load:

```text
Configuration error: Invalid settings in 'deploy_settings':
  settings: Value error, The deployment checks failed:
  security.E001: DEBUG must be False in production
    Hint: DEBUG shows tracebacks and settings to callers.
```

Without `DEBUG`, but with HSTS off, no rate limit, and a cache without
`COMPUTE_TIMEOUT`, the settings load. `check` passes with warnings, and
`check --deploy` fails with status 1:

```text
Error: System check --deploy refuses these warnings:
  security.W002: SECURE_HSTS_SECONDS is 0, so browsers may still use plain HTTP; set it (31536000 is a year) once HTTPS works everywhere.
  security.W003: Requests are not rate limited; set RATE_LIMIT and list domainkit.http.ratelimit.RateLimitMiddleware in MIDDLEWARE.
  caches.W001: CACHES['default'] has no COMPUTE_TIMEOUT, so one get_or_set computation that hangs holds its key until the process restarts; set CACHES['default']['COMPUTE_TIMEOUT'] (REQUEST_TIMEOUT's value, for example).
```

`check` also says, as `security.I001`, what it cannot check: HTTPS. The
note never fails:

```text
  Info: security.I001: HTTPS is not checked: the application serves plain HTTP, and TLS must end in front of it. It takes a request's scheme from the X-Forwarded-Proto header of TRUSTED_PROXIES only, so the proxy must set it, over a caller's.
    Hint: Redirect plain HTTP to HTTPS at the proxy, and keep SECURE_HSTS_SECONDS set (docs/security.md, 'The edge').
```

The production settings that `init` writes start from the safe side:
the API documentation off, HSTS on, a rate limit counted in Redis, and
cache computations bounded.

## Before the first deployment

**Settings and secrets**

- [ ] `ENVIRONMENT = "production"`, so every check below applies. The
  settings `init` writes do this.
- [ ] `DEBUG` off (refused: `security.E001`), and no
  `DebugToolbarMiddleware` (refused: `security.E002`).
- [ ] `SECRET_KEY` of 50 random characters or more, from a file
  (`<PREFIX>SECRET_KEY_FILE`), not from the code. A missing, short,
  weak, or development key is refused (`security.E016` to
  `security.E018`). Replace it through `SECRET_KEY_FALLBACKS`.
- [ ] Every other secret from the environment or a file: the database,
  broker, cache, and ledger credentials, `EVIDENCE.SUBJECT_KEY`, and the
  JWT key. URLs that carry passwords are kept secret in logs and reprs.
- [ ] The JWT key replaced on a schedule through `AUTH.JWT.PREVIOUS_KEYS`.
  It keeps the old key for a token lifetime after the key retires, then
  refuses its tokens. A key that leaked is replaced without it, which
  refuses its tokens at once
  ([authentication.md](auth/authentication.md#replacing-a-key)). A
  previous key that is the signing key, or retires more than a day
  ahead, is refused (`auth.E008`, `auth.E009`).
- [ ] `ALLOWED_HOSTS` names the service's hosts. A wildcard is refused
  (`security.E003`).

**The edge**

- [ ] TLS ends in front of the service, and the proxy redirects plain
  HTTP to HTTPS (`security.I001` says how the scheme is read).
- [ ] `SECURE_HSTS_SECONDS` set (warned when 0: `security.W002`).
- [ ] `TRUSTED_PROXIES` names the proxies' networks only. A network that
  holds every address is refused (`security.E004`). A rate limit with no
  trusted proxy behind a load balancer is warned (`security.W004`).
- [ ] The server run with its own limits and `--no-proxy-headers`
  ([reference/servers.md](reference/servers.md)), behind a proxy that
  bounds headers and slow clients.
- [ ] The proxy sets `X-Request-ID` itself, over a client's line or in a
  line after it, or `ACCEPT_REQUEST_ID = "never"`. Request ids come from
  trusted proxies only, so a caller's own id never names log lines or
  audit rows ([operations.md](operations.md#request-ids)).
- [ ] `RATE_LIMIT` set (warned when missing: `security.W003`), and counted
  in Redis (`RATE_LIMIT_BACKEND = "domainkit.http.ratelimit.RedisBackend"`).
  With the auth app, attempts counted in each process are refused
  (`auth.E105`; `AUTH.REQUIRE_SHARED_THROTTLES = False` warns instead,
  for one process). Password and code checks stop while Redis is down
  (`AUTH.THROTTLE_FAIL_CLOSED`, on by default there): alert on
  `503 throttle_unavailable`
  ([runbook.md](runbook.md#logins-are-refused-while-redis-is-down)).
- [ ] `REQUEST_TIMEOUT` and `MAX_CONCURRENT_REQUESTS` with their
  middleware (warned when missing: `deploy.W001`, `deploy.W002`).
- [ ] `API_DOCS` off, unless the API is public on purpose (warned:
  `security.W001`).
- [ ] `CORS_ALLOWED_ORIGINS` without `*` (warned: `security.W006`).
- [ ] `METRICS_TOKEN` set, so `/metrics` answers only your scraper. In
  staging and production, `/metrics` answers 403 without it.

**Data stores**

- [ ] Every network database with `SSLMODE = "verify-full"` (refused
  otherwise: `security.E007`), and no `ECHO` or `LOG_PARAMETERS`
  (refused: `security.E005`, `security.E006`).
- [ ] The application on a row-level role, and `migrate` on the schema
  owner (`MIGRATION_URL`).
- [ ] Redis over `rediss://` with a password or a client certificate,
  and certificate checks on (refused otherwise: `security.E010` to
  `security.E012`).
- [ ] A broker over `amqps://` with a user of its own, and certificate
  checks on (refused otherwise: `security.E013` to `security.E015`).
- [ ] Backups encrypted, access to them controlled, and a restore tested
  ([operations.md](operations.md#backups-and-restores)).

**Who may do what**

- [ ] A rule on every route, and object rules where users share a table
  ([routes.md](auth/routes.md#protecting-routes)). `REQUIRE_ROUTE_RULES`
  (on by default) refuses a route without one (`security.E101`).
- [ ] Write schemas that hold only the fields clients may set
  (`schema_for(..., write=True, include=[...])`).
- [ ] Staff hold only the permissions they use. The admin API is mounted
  only where staff reach it.
- [ ] Two-factor logins for staff
  ([logging-in.md](auth/logging-in.md#two-factor-logins)), and session
  cookies with the `__Host-` prefix (warned otherwise: `auth.W005`).
- [ ] `LIVE_REAUTH_INTERVAL` set, so WebSockets and event streams drop a
  revoked token (warned when `None`: `security.W005`).

**Evidence and personal data**

- [ ] The audit trail's four pieces: a ledger with TLS and the server's
  signing key (refused without them: `security.E008`, `security.E009`),
  `AUDIT.NAMESPACE`, sealing on a schedule, and the `protect()` triggers.
  Each is refused when missing (`audit.E001` to `audit.E005`), or only
  warned with `AUDIT.REQUIRE_BASELINE = False`
  ([operations.md](operations.md#sealing-the-audit-trail)).
- [ ] `audit verify` daily, with an alert when it fails.
- [ ] Personal data marked, and `privacy purge` on a schedule
  ([privacy.md](privacy.md)).
- [ ] `EVIDENCE.SUBJECT_KEY` set where evidence is kept (refused without:
  `security.E019`).
- [ ] With `domainkit.contrib.keys`: `KEYS.PROVIDER` is `"aws-kms"` or
  `"vault"`. The local key, derived from `SECRET_KEY`, is refused
  (`keys.E001`), and so is Vault over plain HTTP (`keys.E002`)
  ([keys.md](contrib/keys.md#providers)).

**Outside calls**

- [ ] Outgoing calls to URLs that someone else chose (webhooks,
  callbacks) go through a guarded client (`HttpClient(guard=True)`),
  which calls only public addresses
  ([runtime.md](runtime.md#outgoing-http-calls)).
- [ ] No loopback or link-local network in
  `WEBHOOKS.ALLOWED_PRIVATE_NETWORKS` (refused).

## While it runs

- [ ] Alerts on the metrics in
  [operations.md](operations.md#metrics-to-alert-on), each with its
  [runbook](runbook.md) entry.
- [ ] Patch releases taken as they come. They carry security fixes only
  for the latest minor ([versioning.md](versioning.md#security-fixes-and-support)).
- [ ] `pip-audit` (or the platform's scanner) over the deployed image's
  packages, on a schedule: new advisories appear with no change on your
  side. The CI that `init` writes runs `pip-audit` on every push.
- [ ] The image built from `requirements.lock` by its hashes, on a base
  image pinned by digest ([deploying.md](deploying.md#before-the-first-deployment)).

## Operators and commands

People who run `manage.py` in production act outside the application.
domainkit names them and records what they do in the audit trail (with
`domainkit.contrib.audit` installed).

- [ ] Operators named. Commands that change data or credentials
  (`messages retry`, `audit forget`, `flush`, `privacy purge`, ...) take
  `--operator NAME`, or read `DOMAINKIT_OPERATOR`. Set
  `DOMAINKIT_OPERATOR` from your access tooling: in a container that
  runs as `root` or `app`, the login name names nobody
  ([reference/commands.md](reference/commands.md#operators)).
- [ ] `loaddata` and `flush` refuse to run without a name given on
  purpose (`--operator` or `DOMAINKIT_OPERATOR`), and need `--force`.
- [ ] `loaddata --include-guarded` and `flush --include-guarded` kept for
  a restore you planned. Without the flag, users, permissions, grants,
  tokens, and second factors are left alone
  ([reference/commands.md](reference/commands.md#data-fixtures-flush-and-seeds)).
- [ ] Consoles recorded. `shell` and `dbshell` take `--operator`, and in
  staging and production each one is recorded as it opens
  (`operator.shell`, `operator.dbshell`), before the operator types
  anything. What a `dbshell` client does is beyond the trail.
  `dbshell` in production also needs `--allow-production`.
- [ ] `seed` run as a named operator: what it writes is audited, and the
  run is recorded (`operator.seed`).
- [ ] Downgrades guarded. In staging and production,
  `migrate --downgrade` needs `--allow-data-loss`, a tenant's schema
  included. Each downgrade is recorded (`operator.migrate.downgrade`)
  before it runs, so a downgrade that drops the trail's own tables is
  still on record ([operations.md](operations.md#rolling-back-a-migration)).
- [ ] Who may run `manage.py` against production kept as narrow as who
  may reach its database. An operator's name attributes an action; it
  does not authenticate anyone.

## Verifying a release

Each domainkit release on PyPI carries SLSA build provenance for the
wheel and the sdist, and a CycloneDX bill of materials attested for the
wheel. Check a file before you deploy it:

```bash
gh attestation verify domainkit-<version>-py3-none-any.whl --repo karimmattar/domainkit
```

The release is built in CI from `main` ([SECURITY.md](../SECURITY.md)).
The tools that build it (poetry and what it needs) are installed from a
lock, `.github/requirements/build-tools.txt`, each file checked against
its hash. So a new release of one of those tools cannot change what is
built.

## Common mistakes

- **Silencing a warning to make the pipeline pass.** List a warning in
  `SILENCED_CHECKS` only once you have judged it, such as `API_DOCS` on
  an API that is public on purpose.
- **Trusting every proxy.** `TRUSTED_PROXIES = ["0.0.0.0/0"]` lets any
  caller choose its address and its rate limit, and is refused. List the
  load balancers' networks.
- **Rate limits counted in each process.** A guesser gets the rates once
  per process. Count them in Redis.
- **One shared operator name.** `root` or `app` names nobody. Give each
  person a name of their own.
- **Turning `AUTH.THROTTLE_FAIL_CLOSED` off and forgetting it.**
  Passwords can then be guessed without a limit while Redis is down. Set
  it back once Redis answers.

## See also

- [Threat model](threat-model.md): why each item matters.
- [Deployment checks](reference/checks.md): every check, refused or
  warned.
- [Deploying](deploying.md): the deployment checklist.
- [Operations](operations.md): configuration, operators, and backups.
- [SECURITY.md](../SECURITY.md): reporting a vulnerability.
