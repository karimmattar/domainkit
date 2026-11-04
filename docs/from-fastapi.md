# Coming from FastAPI

domainkit is built on FastAPI: a route is still a FastAPI path operation,
on an `APIRouter`, with Pydantic at its edges. What it adds is where the
rest goes, and its own names for it. This page maps what a FastAPI user
reaches for to its name here, and the page that says how.
[The tutorial](tutorial.md) shows them working together.

## The application and its settings

| FastAPI | domainkit |
| --- | --- |
| `app = FastAPI()` in `main.py` | `domainkit init`: the application is built from the settings, routers mounted in `src/config/urls.py` ([tutorial](tutorial/0-first-route.md)) |
| `APIRouter`, `include_router` | The same; a domain's routes are in its `routes.py` ([generators](resources.md#generators)) |
| `uvicorn main:app --reload` | `manage.py runserver` in development; uvicorn or granian in production ([servers](reference/servers.md)) |
| `pydantic-settings`, environment variables | `config/settings/{base,local,test,production}.py`, `domainkit.conf.Env` ([operations.md](operations.md#configuration)) |
| Startup and shutdown in `lifespan` | `on_startup` and `on_shutdown`, registered in a domain's `DomainConfig.ready()` ([api/core.md](api/core.md#domainkitapps)) |

## Data

| FastAPI with SQLAlchemy | domainkit |
| --- | --- |
| A declarative model and its table | An aggregate (`AggregateRoot`), plain Python, and its table from its fields (`table_for`) ([databases.md](databases.md#tables-from-domain-classes)) |
| `AsyncSession`, `async with session.begin():` | A unit of work: `async with uow: ... await uow.commit()` ([databases.md](databases.md#transactions)) |
| `session.execute(select(...).where(...))` | `repository.filter(F("name") == ...)`, the same in SQL and in memory ([queries.md](queries.md#querying-a-repository)) |
| `alembic revision --autogenerate`, `alembic upgrade head` | `makemigrations`, `migrate`, on Alembic ([operations.md](operations.md#migrations-in-a-rollout)) |

## The API

| FastAPI | domainkit |
| --- | --- |
| A path operation per action on a model | `ResourceRouter`: reads, and one route per command ([resources.md](resources.md)) |
| Pydantic request and response models | A command made from a use case's parameters, and `schema_for(Model)` ([resources.md](resources.md#schemas-from-the-domain)) |
| `Depends(get_current_user)`, `Security(...)` | An access rule on every route: `Public`, `Require(...)`; `check` lists a route without one, and staging and production refuse to start ([auth](auth/routes.md#protecting-routes)) |
| `OAuth2PasswordBearer`, fastapi-users | `domainkit.contrib.auth`: users, tokens, JWTs, cookie sessions ([auth.md](auth.md)) |
| `raise HTTPException(404)` | A domain error (`NotFound`, `BusinessRuleViolation`), answered as `application/problem+json` with a stable `code` ([problems](reference/problems.md)) |
| slowapi | `RATE_LIMIT`, and throttles for one route ([cache.md](cache.md#throttles-for-one-route)) |
| `BackgroundTasks`, Celery | `@task`, `uow.defer(...)`, `@periodic`, run by `runworker` from an outbox ([messaging](contrib/messaging.md)) |
| `TestClient` | pytest fixtures: `client`, `admin_client`, `client_for` ([testing.md](testing.md)) |

What has no FastAPI name: aggregates and their rules, use cases, the
transactional outbox, the audit trail, and idempotency keys.
[learning.md](learning.md#words) gives the words, and
[resources.md](resources.md) starts there.
