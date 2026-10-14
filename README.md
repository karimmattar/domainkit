# domainkit

A Django-style framework for domain-driven FastAPI services. It provides:

- settings modules and installed domains
- `manage.py` commands and migrations
- DDD building blocks, with the layer rules between them enforced
- several databases: domains routed to their own, sharding by key,
  PostgreSQL range partitioning, and immudb ledgers for evidence
- caches in memory or in Redis and Valkey, with stampede protection, and
  rate limits shared by every replica
- optional apps for reliable messaging, audit trails, idempotent requests,
  and tamper-evident evidence

```bash
pip install "domainkit[postgres]"     # extras: postgres, sqlite, immudb, redis, metrics, tracing, testing
```

## Documentation

- [docs/reference.md](https://github.com/karimmattar/domainkit/blob/main/docs/reference.md): settings, middleware, errors,
  observability, testing, commands, and extension points
- [docs/databases.md](https://github.com/karimmattar/domainkit/blob/main/docs/databases.md): several databases, routing,
  sharding, partitioning, and ledgers
- [docs/cache.md](https://github.com/karimmattar/domainkit/blob/main/docs/cache.md): caches in memory, Redis, and Valkey,
  and shared rate limits
- [docs/contrib.md](https://github.com/karimmattar/domainkit/blob/main/docs/contrib.md): messaging, audit, idempotency, and evidence
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
pytest
python manage.py runserver            # http://127.0.0.1:8000/docs
```

`domainkit init` creates a project that passes `check` and its tests as it is:

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

The production settings read `BILLING_API_ALLOWED_HOSTS` and
`BILLING_API_DATABASE_URL` from the environment, and the deployment checks
refuse to start without them, or without verified TLS to the database.

## Developing domainkit

```bash
poetry install --all-extras            # Python 3.13
pre-commit install                     # after: pipx install pre-commit

poetry run ruff check . && poetry run ruff format --check . && poetry run mypy
poetry run pytest --cov                # random order; coverage gate in pyproject.toml
poetry run mutmut run "domainkit.http*" && poetry run mutmut results
```

The database tests use SQLite unless `DOMAINKIT_TEST_POSTGRES_URL` names a
PostgreSQL server; each test then creates a database there and drops it.
Use a throwaway server:

```bash
docker run -d --rm --name domainkit-postgres -e POSTGRES_PASSWORD=postgres \
  -p 127.0.0.1:55432:5432 postgres:18-alpine
DOMAINKIT_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
  poetry run pytest
```

CI (`.github/workflows/ci.yml`) runs the same checks on every push and pull
request:

- lint and strict typing
- the tests on Python 3.13 and 3.14
- the tests again with PostgreSQL 18, including row locking, the audit
  triggers, and server-side timeouts
- a wheel build, installed and imported in a clean environment
- pip-audit, and a gitleaks scan of the whole history

A full mutation testing run happens weekly. Releases are published to PyPI
from a version tag; see [docs/versioning.md](https://github.com/karimmattar/domainkit/blob/main/docs/versioning.md).

## License

MIT; see [LICENSE](https://github.com/karimmattar/domainkit/blob/main/LICENSE).
