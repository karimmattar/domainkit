<p align="center">
  <img src="https://raw.githubusercontent.com/karimmattar/domainkit/main/docs/assets/domainkit-banner.jpg" alt="domainkit, a domain-driven FastAPI framework" width="720">
</p>

# domainkit

A Django-style framework for domain-driven FastAPI services. It provides:

- settings modules and installed domains
- `manage.py` commands and migrations
- DDD building blocks, with the layer rules between them enforced
- several databases: domains routed to their own, sharding by key,
  PostgreSQL range partitioning, and immudb ledgers for evidence
- caches in memory or in Redis and Valkey (one server, Sentinel, or
  Cluster), with stampede protection, and rate limits shared by every
  replica, globally or per route
- answers in the caller's language (English and Arabic ship), times shown
  in the business's time zone, and numbers and dates in local formats
- file storage and uploads checked by their content, under names the
  storage makes
- background work through a transactional outbox, run by polling workers or
  through RabbitMQ, with queues for separate worker pools, and ordering
  keys that run one key's messages one at a time, in commit order
- users, groups, and permissions per content type, as in Django's
  `contrib.auth`: password hashing and rules, backends, object permissions,
  and a swappable user model; token or JWT authentication, and route rules
  as in Django REST framework
- optional apps for reliable messaging, audit trails, idempotent requests,
  tamper-evident evidence, signed outgoing webhooks, feature flags, and privacy
  requests (export, erasure, and retention)

```bash
pip install "domainkit[postgres]"     # the extras are listed below
```

The extras: `postgres`, `sqlite` (database drivers); `argon2`, `jwt`, `mfa`
(passwords, JSON Web Tokens, and second factors for `contrib.auth`);
`immudb` (ledgers); `redis` (shared caches and rate limits); `rabbitmq`
(the worker's broker); `metrics`, `tracing` (Prometheus and
OpenTelemetry); `uploads` (multipart forms); `httpclient` (outgoing HTTP
calls); `webhooks`; `testing` (the pytest plugin); `tzdata` (time zones
where the system has none); `uvloop` and `granian` (a faster event loop,
and a server).

## Documentation

- [docs/tutorial.md](https://github.com/karimmattar/domainkit/blob/main/docs/tutorial.md): getting started, in five
  parts: a first resource, rules, permissions, events and the worker, and
  tests and production; every command and code change run in CI
- [docs/operations.md](https://github.com/karimmattar/domainkit/blob/main/docs/operations.md): running in production:
  processes, configuration, sizing, migrations in a rollout, the worker,
  sealing, backups, and what to alert on; [docs/runbook.md](https://github.com/karimmattar/domainkit/blob/main/docs/runbook.md)
  for each alarm, [docs/security.md](https://github.com/karimmattar/domainkit/blob/main/docs/security.md) the hardening
  checklist, and [docs/upgrading.md](https://github.com/karimmattar/domainkit/blob/main/docs/upgrading.md) from one minor to
  the next

- [docs/reference.md](https://github.com/karimmattar/domainkit/blob/main/docs/reference.md): settings, deployment checks,
  middleware, errors, the API surface (OpenAPI, versions, deprecations),
  observability, testing, commands, and extension points, a page for each
- [docs/databases.md](https://github.com/karimmattar/domainkit/blob/main/docs/databases.md): several databases, routing,
  sharding, partitioning, and ledgers
- [docs/queries.md](https://github.com/karimmattar/domainkit/blob/main/docs/queries.md): querying repositories,
  pages, filters, soft delete, row locks, and savepoints
- [docs/resources.md](https://github.com/karimmattar/domainkit/blob/main/docs/resources.md): aggregates over REST:
  resource routes, commands and queries, derived schemas, partial changes,
  ETags and conditional writes, field policies, nesting, the generators,
  and the generic admin
- [docs/runtime.md](https://github.com/karimmattar/domainkit/blob/main/docs/runtime.md): how async work runs
  (event loops, background tasks, clean contexts, logging off the loop,
  finding blocking code, free-threaded Python, the shutdown budget,
  readiness, event-handler timeouts, workers and the outbox), running under
  load (request deadlines, load shedding, database timeouts and PgBouncer,
  outgoing HTTP calls), and servers
- [docs/cache.md](https://github.com/karimmattar/domainkit/blob/main/docs/cache.md): caches in memory, Redis, and Valkey,
  and shared rate limits
- [docs/contrib.md](https://github.com/karimmattar/domainkit/blob/main/docs/contrib.md): messaging (and workers on RabbitMQ),
  webhooks, audit and records' history, idempotency, evidence, and
  feature flags
- [docs/auth.md](https://github.com/karimmattar/domainkit/blob/main/docs/auth.md): users, groups, permissions, content
  types, passwords, backends, tokens, route rules, and the admin API
- [docs/mail.md](https://github.com/karimmattar/domainkit/blob/main/docs/mail.md): email, mail templates, text messages,
  and push notifications, sent through the outbox
- [docs/i18n.md](https://github.com/karimmattar/domainkit/blob/main/docs/i18n.md): responses in the caller's language,
  time zones, and number and date formats
- [docs/uploads.md](https://github.com/karimmattar/domainkit/blob/main/docs/uploads.md): file storages, receiving uploads,
  and serving files
- [docs/live.md](https://github.com/karimmattar/domainkit/blob/main/docs/live.md): WebSockets and Server-Sent Events,
  bounded and re-authorized while open, and committed events streamed live
- [docs/privacy.md](https://github.com/karimmattar/domainkit/blob/main/docs/privacy.md): exporting and erasing a person's
  data, and retention
- [docs/testing.md](https://github.com/karimmattar/domainkit/blob/main/docs/testing.md): the pytest plugin: test databases,
  sync and async clients, factories, a clock to freeze, and query counts
- [docs/performance.md](https://github.com/karimmattar/domainkit/blob/main/docs/performance.md): what a request costs, the
  benchmark ladder CI gates on, and how to make requests cheaper
- [docs/versioning.md](https://github.com/karimmattar/domainkit/blob/main/docs/versioning.md): compatibility, deprecations,
  releases, and which releases get security fixes
- [docs/threat-model.md](https://github.com/karimmattar/domainkit/blob/main/docs/threat-model.md): what domainkit protects,
  where the trust boundaries are, what it does at each, and what is left to
  a project
- [docs/fintech.md](https://github.com/karimmattar/domainkit/blob/main/docs/fintech.md): a bank's first review, answered: each
  control's page, setting, and test, and `domainkit init --profile fintech`,
  a project with them all on and wired
- [docs/audit/0.9.0.md](https://github.com/karimmattar/domainkit/blob/main/docs/audit/0.9.0.md): the 0.9.0 audit's findings
  that the CHANGELOG names, with their status
- [docs/api.md](https://github.com/karimmattar/domainkit/blob/main/docs/api.md): the public API, module by module, one line a name;
  `mkdocs.yml` builds these pages into a site, with every name's docstring
- [docs/adr/](https://github.com/karimmattar/domainkit/tree/main/docs/adr): the decisions behind the design
- [CHANGELOG.md](https://github.com/karimmattar/domainkit/blob/main/CHANGELOG.md) and [SECURITY.md](https://github.com/karimmattar/domainkit/blob/main/SECURITY.md)

## Quick start

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install "domainkit[postgres,redis,sqlite,testing]"
domainkit init billingAPI billing     # the name sets the variable prefix: BILLING_API_*
cd billing
python manage.py check
python manage.py startdomain invoices --aggregate Invoice --minimal   # installs and mounts it
python manage.py makemigrations -m "create invoices" && python manage.py migrate
read -rsp "Password: " PASSWORD && export PASSWORD    # admin's, typed, not echoed
BILLING_API_SUPERUSER_PASSWORD="$PASSWORD" \
  python manage.py createsuperuser --noinput --username admin --email admin@example.com
unset PASSWORD                        # so that no later process inherits it
pytest
python manage.py runserver            # http://127.0.0.1:8000/docs
```

Then, from a second shell in `billing` with the same environment active, log
in, typing the same password, and list the invoices. The password is read
at a prompt and sent on standard input, so it is in no command line and no
shell history:

```bash
read -rsp "Password: " PASSWORD && export PASSWORD
TOKEN=$(python -c "import json, os; print(json.dumps({'username': 'admin', 'password': os.environ['PASSWORD']}))" \
  | curl -s http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" -d @- \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
unset PASSWORD
curl http://127.0.0.1:8000/api/v1/invoices/invoices -H "Authorization: Bearer $TOKEN"
```

The answer is the first page, empty: `{"items":[],"next_cursor":null}`. The
domain's router is mounted at `/api/v1/invoices`, and its resource is
`/invoices` within it; `startdomain` prints both commands for the domain it
made. `--minimal` keeps the domain small: an `Invoice` with a name that
must not be blank, a command to create one and one to rename it, its
table, its resource, and two tests, in 13 files and the four layers `check`
holds apart. `startaggregate`, `addcommand`, `addquery`, and `addevent`
grow it. Without `--minimal`, `startdomain` writes a fuller example (notes
as child entities, an owner, events and a handler, filters); the tutorial
starts from that one. A route the caller may not reach answers 404, as an unknown path does,
so check the path first (`/docs` lists every route). `createsuperuser`
without options asks for each value instead. [docs/tutorial.md](https://github.com/karimmattar/domainkit/blob/main/docs/tutorial.md)
goes on from here, in five parts: rules written by hand, permissions,
events run by the worker, and tests.

`domainkit init` creates a project that passes `check` and its tests as it is.
Users, groups, and permissions are installed, requests are authenticated,
and every route must say who may call it: generated domains need their
model permissions, and `/auth` and `/admin/auth` log users in and manage
them ([docs/auth.md](https://github.com/karimmattar/domainkit/blob/main/docs/auth.md)).


```text
billing/
  pyproject.toml          the name, dependencies, and the pytest plugin
  manage.py
  .env                    selects config.settings.local on this machine
  .gitignore
  Dockerfile              the image: uvicorn, as a user without privileges
  .dockerignore
  .github/workflows/ci.yml   ruff, check, migrations, tests, and the image
  src/config/settings/    base, local, test, and production settings
  src/config/urls.py      the root router: mount each domain's router here
  src/config/asgi.py      the application, for uvicorn
  src/domains/            the bounded contexts
  tests/test_health.py
```

The production settings read `BILLING_API_ALLOWED_HOSTS`,
`BILLING_API_DATABASE_URL`, `BILLING_API_REDIS_URL` (where requests and
login attempts are counted, once for every process), and
`BILLING_API_TRUSTED_PROXIES` (the load balancers' networks, or empty)
from the environment, and the deployment checks refuse to start without
them, or without verified TLS to the database. They
also turn the API docs off, send HSTS for a year, and limit each caller to
300 requests a minute; `BILLING_API_API_DOCS`,
`BILLING_API_SECURE_HSTS_SECONDS`, and `BILLING_API_RATE_LIMIT` change that.

Serve it with one worker process per core, each on its own event loop:
`uvicorn config.asgi:application --workers 4 --loop uvloop`, or Granian
(`pip install 'domainkit[granian]'`). [docs/reference/servers.md](https://github.com/karimmattar/domainkit/blob/main/docs/reference/servers.md)
gives the whole command with its limits, and [docs/performance.md](https://github.com/karimmattar/domainkit/blob/main/docs/performance.md)
how many cores a load needs.

## Developing domainkit

[CONTRIBUTING.md](https://github.com/karimmattar/domainkit/blob/main/CONTRIBUTING.md)
says how to set up, which checks a change must pass, and how to write it
up. In short:

```text
make install        # Python 3.13, Poetry 2.2: every extra, dev and docs groups, hooks
make check          # lint, strict types, deptry, the tests, the docs site
make integration    # the tests against the servers of compose.yaml, as CI runs them
make                # every command
```

Without servers, the database tests use SQLite, the Redis and RabbitMQ
tests use in-memory fakes, and the tests that need PostgreSQL, immudb, or a
real Redis, Valkey, or RabbitMQ are skipped. `compose.yaml` starts the
servers CI's integration job uses, at the same images and ports, with
throwaway certificates in `.ci/`; `make integration` sets the variables
that point the tests at them (the `DOMAINKIT_TEST_*` names at the top of
`domainkit_tests/test_immudb.py`, `test_redis.py`, and `test_rabbitmq.py`).
Each run must cover 98%; CI also combines the runs with and without
servers, which must cover 100% (`make cov`).

CI (`.github/workflows/ci.yml`) runs on every push and pull request, the
suite on every core of the runner (`pytest -n auto`):

- lint, strict typing, and deptry: every package the code imports is declared
- the tests on Python 3.13 and 3.14
- the tests again against PostgreSQL 18, immudb with TLS and a signing key,
  Redis 8 (plain and TLS), Valkey 9, and RabbitMQ 4 (plain and TLS); no test
  may be skipped there
- coverage of both runs combined, which must be 100%
- the benchmark ladder: Python calls and database round trips per request,
  against budgets (`python -m benchmarks`; docs/performance.md)
- a wheel build, installed and imported in a clean environment, and its
  CycloneDX bill of materials
- Semgrep's Python and OWASP Top 10 rules, zizmor over the workflows,
  pip-audit, and a gitleaks scan of the whole history; CodeQL's security
  queries in a workflow of their own (`codeql.yml`)
- the documentation site, built with every link and public name checked
- on pull requests and weekly, the tests again with the newest releases
  the dependency ranges allow, not the lock's, and with the oldest

A pull request that changes only `docs/` and the Markdown files at the top
of the repository runs lint, the audits, and the tests that read the docs,
not the whole suite. A release reuses the run of its commit's push to
`main` instead of running it again.

A full mutation testing run happens weekly, in 17 shards that run side by
side; each writes its score and the mutants no test caught to its job
summary. Releases are published to PyPI
from a version tag; see [docs/versioning.md](https://github.com/karimmattar/domainkit/blob/main/docs/versioning.md).

## License

MIT; see [LICENSE](https://github.com/karimmattar/domainkit/blob/main/LICENSE).
