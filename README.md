# domainkit

A Django-style framework for domain-driven FastAPI services. It provides:

- settings modules and installed domains
- `manage.py` commands and migrations
- DDD building blocks, with the layer rules between them enforced
- optional apps for reliable messaging, audit trails, and idempotent requests

```bash
pip install "domainkit[postgres]"     # extras: postgres, sqlite, metrics, tracing, testing
```

## Documentation

- [docs/reference.md](https://github.com/karimmattar/domainkit/blob/main/docs/reference.md): settings, middleware, errors,
  observability, testing, commands, and extension points
- [docs/contrib.md](https://github.com/karimmattar/domainkit/blob/main/docs/contrib.md): messaging, audit, and idempotency
- [docs/versioning.md](https://github.com/karimmattar/domainkit/blob/main/docs/versioning.md): compatibility, deprecations, and releases
- [docs/adr/](https://github.com/karimmattar/domainkit/tree/main/docs/adr): the decisions behind the design
- [CHANGELOG.md](https://github.com/karimmattar/domainkit/blob/main/CHANGELOG.md) and [SECURITY.md](https://github.com/karimmattar/domainkit/blob/main/SECURITY.md)

## Quick start

A project is a `pyproject.toml`, a `manage.py`, a `config` package, and a
`domains` package for its bounded contexts. The project's name sets the
environment variable prefix: `billingAPI` reads `BILLING_API_*`.

```text
billing/
  pyproject.toml
  manage.py
  src/config/__init__.py
  src/config/settings/__init__.py
  src/config/settings/local.py
  src/config/settings/test.py
  src/config/urls.py
  src/config/asgi.py
  src/domains/__init__.py
```

```toml
# pyproject.toml
[project]
name = "billingAPI"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["domainkit[sqlite]"]

[dependency-groups]
dev = ["domainkit[testing]"]                # pytest and httpx2, for the fixtures

[tool.pytest.ini_options]
pythonpath = ["src"]
addopts = "-p domainkit.testing"           # the database and client fixtures
domainkit_settings = "config.settings.test"
```

```python
# manage.py
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from domainkit.management import execute_from_command_line

    execute_from_command_line(sys.argv)
```

```python
# src/config/settings/local.py
from pathlib import Path

from domainkit.conf.env import Env

env = Env()
BASE_DIR = Path(__file__).resolve().parents[3]

DEBUG = True
INSTALLED_DOMAINS = ["domainkit.contrib.messaging"]
ROOT_ROUTER = "config.urls.router"
ASGI_APPLICATION = "config.asgi:application"
DATABASES = {
    "default": {
        "URL": env.str("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'db.sqlite3'}")
    },
}
```

```python
# src/config/settings/test.py
from config.settings.local import *

DEBUG = False
ALLOWED_HOSTS = ["testserver"]
DATABASES = {"default": {"URL": "sqlite+aiosqlite:///:memory:"}}
```

```python
# src/config/urls.py
from fastapi import APIRouter

from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
```

```python
# src/config/asgi.py
from domainkit.http import get_asgi_application

application = get_asgi_application()
```

Then:

```bash
pip install "domainkit[sqlite,testing]"                    # in a virtual environment
export BILLING_API_SETTINGS_MODULE=config.settings.local   # or put it in .env
python manage.py check
python manage.py startdomain invoices --aggregate Invoice   # prints how to install it
python manage.py makemigrations -m "create invoices" && python manage.py migrate
pytest                                                      # the generated tests
python manage.py runserver                                  # http://127.0.0.1:8000/docs
```

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
