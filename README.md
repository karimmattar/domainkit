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
  through RabbitMQ, with queues for separate worker pools
- users, groups, and permissions per content type, as in Django's
  `contrib.auth`: password hashing and rules, backends, object permissions,
  and a swappable user model; token or JWT authentication, and route rules
  as in Django REST framework
- optional apps for reliable messaging, audit trails, idempotent requests,
  tamper-evident evidence, signed outgoing webhooks, and privacy requests
  (export, erasure, and retention)

```bash
pip install "domainkit[postgres]"     # extras: postgres, sqlite, argon2, jwt, immudb, redis, rabbitmq, metrics, tracing, uploads, testing, webhooks, tzdata
```

## Documentation

- [docs/reference.md](https://github.com/karimmattar/domainkit/blob/main/docs/reference.md): settings, middleware, errors,
  observability, testing, commands, and extension points
- [docs/databases.md](https://github.com/karimmattar/domainkit/blob/main/docs/databases.md): several databases, routing,
  sharding, partitioning, and ledgers
- [docs/cache.md](https://github.com/karimmattar/domainkit/blob/main/docs/cache.md): caches in memory, Redis, and Valkey,
  and shared rate limits
- [docs/contrib.md](https://github.com/karimmattar/domainkit/blob/main/docs/contrib.md): messaging (and workers on RabbitMQ),
  webhooks, audit, idempotency, and evidence
- [docs/auth.md](https://github.com/karimmattar/domainkit/blob/main/docs/auth.md): users, groups, permissions, content
  types, passwords, backends, tokens, route rules, and the admin API
- [docs/i18n.md](https://github.com/karimmattar/domainkit/blob/main/docs/i18n.md): responses in the caller's language,
  time zones, and number and date formats
- [docs/uploads.md](https://github.com/karimmattar/domainkit/blob/main/docs/uploads.md): file storages, receiving uploads,
  and serving files
- [docs/privacy.md](https://github.com/karimmattar/domainkit/blob/main/docs/privacy.md): exporting and erasing a person's
  data, and retention
- [docs/testing.md](https://github.com/karimmattar/domainkit/blob/main/docs/testing.md): the pytest plugin: test databases,
  sync and async clients, factories, a clock to freeze, and query counts
- [docs/versioning.md](https://github.com/karimmattar/domainkit/blob/main/docs/versioning.md): compatibility, deprecations, and releases
- [docs/adr/](https://github.com/karimmattar/domainkit/tree/main/docs/adr): the decisions behind the design
- [CHANGELOG.md](https://github.com/karimmattar/domainkit/blob/main/CHANGELOG.md) and [SECURITY.md](https://github.com/karimmattar/domainkit/blob/main/SECURITY.md)

## Quick start

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install "domainkit[postgres,sqlite,testing]"
domainkit init billingAPI billing     # the name sets the variable prefix: BILLING_API_*
cd billing
python manage.py check
python manage.py startdomain invoices --aggregate Invoice   # prints how to install it
python manage.py makemigrations -m "create invoices" && python manage.py migrate
python manage.py createsuperuser
pytest
python manage.py runserver            # http://127.0.0.1:8000/docs
```

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
  src/config/settings/    base, local, test, and production settings
  src/config/urls.py      the root router: mount each domain's router here
  src/config/asgi.py      the application, for uvicorn
  src/domains/            the bounded contexts
  tests/test_health.py
```

The production settings read `BILLING_API_ALLOWED_HOSTS`,
`BILLING_API_DATABASE_URL`, and `BILLING_API_TRUSTED_PROXIES` (the load
balancers' networks, or empty) from the environment, and the deployment
checks refuse to start without them, or without verified TLS to the database. They
also turn the API docs off, send HSTS for a year, and limit each caller to
300 requests a minute; `BILLING_API_API_DOCS`,
`BILLING_API_SECURE_HSTS_SECONDS`, and `BILLING_API_RATE_LIMIT` change that.

## Developing domainkit

```bash
poetry install --all-extras            # Python 3.13
pre-commit install                     # after: pipx install pre-commit

poetry run ruff check . && poetry run ruff format --check . && poetry run mypy
poetry run pytest --cov                # random order; coverage gate in pyproject.toml
poetry run mutmut run "domainkit.http*" && poetry run mutmut results
```

Without servers, the database tests use SQLite, the Redis and RabbitMQ
tests use in-memory fakes, and the tests that need PostgreSQL, immudb, or a
real Redis, Valkey, or RabbitMQ are skipped. Point them at throwaway servers to run
them; each test cleans up after itself:

```bash
docker run -d --rm --name domainkit-postgres -e POSTGRES_PASSWORD=postgres \
  -p 127.0.0.1:55432:5432 postgres:18-alpine
docker run -d --rm --name domainkit-redis -p 127.0.0.1:56379:6379 redis:8-alpine
DOMAINKIT_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
DOMAINKIT_TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  poetry run pytest
```

The other variables (immudb, Redis over TLS, Valkey, RabbitMQ) are listed
at the top of `domainkit_tests/test_immudb.py`, `test_redis.py`, and
`test_rabbitmq.py`. Each run must cover
98%; CI also combines the runs with and without servers, which must cover
100%. To check that locally, run `pytest --cov` once without and once with
the servers, each with its own `COVERAGE_FILE`, then `coverage combine` and
`coverage report --fail-under=100`.

CI (`.github/workflows/ci.yml`) runs on every push and pull request:

- lint and strict typing
- the tests on Python 3.13 and 3.14
- the tests again against PostgreSQL 18, immudb with TLS and a signing key,
  Redis 8 (plain and TLS), Valkey 9, and RabbitMQ 4 (plain and TLS); no test
  may be skipped there
- coverage of both runs combined, which must be 100%
- a wheel build, installed and imported in a clean environment
- pip-audit, and a gitleaks scan of the whole history

A full mutation testing run happens weekly. Releases are published to PyPI
from a version tag; see [docs/versioning.md](https://github.com/karimmattar/domainkit/blob/main/docs/versioning.md).

## License

MIT; see [LICENSE](https://github.com/karimmattar/domainkit/blob/main/LICENSE).
