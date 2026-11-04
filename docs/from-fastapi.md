# Coming from FastAPI

domainkit is built on FastAPI. A route is still a FastAPI path
operation, on an `APIRouter`, with Pydantic at its edges. What domainkit
adds is a place for everything else, and names for those places. This
page maps what a FastAPI user knows to its name in domainkit. Each row
links to the page that explains it. [The tutorial](tutorial.md) shows
the pieces working together.

On this page you find:

- the tables, one for each area of a FastAPI service;
- the ideas that have no FastAPI name.

## The biggest difference

A FastAPI service often puts its rules in the path operations, beside
the SQL. In domainkit, the path operation only translates HTTP:

- an [aggregate](learning.md#aggregate) holds the business rules, in
  plain Python, with no database and no HTTP;
- a [use case](learning.md#use-case) loads aggregates, calls them, and
  commits, in one [unit of work](learning.md#unit-of-work);
- a `ResourceRouter` makes the routes from the use cases.

The API, a script, and a test all call the same use case, so all of
them meet the same rules. Your own path operations still work, next to
the generated ones ([tutorial, part 0](tutorial/0-first-route.md)).

## The application and its settings

| FastAPI | domainkit |
| --- | --- |
| `app = FastAPI()` in `main.py` | `domainkit init` writes the project; `get_asgi_application()` builds the application from the settings ([tutorial, part 0](tutorial/0-first-route.md)) |
| `APIRouter`, `include_router` | The same; the project's routers are mounted in `src/config/urls.py`, and a domain's routes are in its `routes.py` ([generators](resources.md#generators)) |
| `uvicorn main:app --reload` | `python manage.py runserver` in development; uvicorn or Granian in production ([servers](reference/servers.md)) |
| `pydantic-settings`, environment variables | Settings modules, `config/settings/{base,local,test,production}.py`, and `Env` from `domainkit.conf` ([configuration](operations.md#configuration)) |
| `lifespan`, startup and shutdown events | `on_startup` and `on_shutdown`, registered in a domain's `DomainConfig.ready()` ([public API](api/core.md)) |
| `app.add_middleware(...)` | The `MIDDLEWARE` setting, in a fixed order ([middleware](reference/http.md#middleware)) |
| `CORSMiddleware` | The `CORS_*` settings ([HTTP settings](reference/http.md#http)) |
| `GZipMiddleware` | `GZIP_MINIMUM_SIZE` and `GZIP_LEVEL` ([HTTP settings](reference/http.md#http)) |
| `TrustedHostMiddleware` | `ALLOWED_HOSTS` ([HTTP settings](reference/http.md#http)) |
| `request.client.host` behind a proxy, `--proxy-headers` | `TRUSTED_PROXIES` ([running behind a proxy](recipes/behind-a-proxy.md)) |
| `/docs`, `/redoc`, `/openapi.json` | The same pages, on while `DEBUG` is on; `API_DOCS` and `manage.py openapi` ([HTTP settings](reference/http.md#http)) |
| A health endpoint of your own | `/health` and `/ready`, built in ([readiness](runtime.md#readiness)) |

## Data

| FastAPI with SQLAlchemy or SQLModel | domainkit |
| --- | --- |
| A declarative model, or a `SQLModel` table | An aggregate (`AggregateRoot`), plain Python, and its table from its fields (`table_for`) ([tables from domain classes](databases.md#tables-from-domain-classes)) |
| `Depends(get_session)` | A unit of work, given to each use case ([use cases](resources.md#use-cases)) |
| `AsyncSession`, `async with session.begin():` | A unit of work: `async with uow: ...` then `await uow.commit()` ([transactions](databases.md#transactions)) |
| `session.execute(select(...).where(...))` | `repository.filter(F("name") == ...)`, the same in SQL and in memory ([querying a repository](queries.md#querying-a-repository)) |
| `.with_for_update()` | `get_for_update` and `RowLock` ([row locks](queries.md#row-locks)) |
| `alembic revision --autogenerate`, `alembic upgrade head` | `makemigrations` and `migrate`, on Alembic ([migrations in a rollout](operations.md#migrations-in-a-rollout)) |
| Several engines, read replicas | `DATABASES`, `DATABASE_ROUTING`, and `REPLICAS` ([databases](databases.md)) |
| `Decimal` for money | `Money`, an exact amount and its currency ([money](money.md)) |

## The API

| FastAPI | domainkit |
| --- | --- |
| A path operation for each action on a model | `ResourceRouter`: the reads, and one route for each use case ([resources](resources.md)) |
| Pydantic request models | The command that a use case's parameters make ([use cases](resources.md#use-cases)) |
| `response_model` | `schema_for(Model)` ([schemas from the domain](resources.md#schemas-from-the-domain)) |
| `Field(max_length=...)`, `conint(ge=0)` | Markers on the aggregate's fields: `MaxLength`, `Range`, and others, checked in the object, the request, and the table ([validation](validation.md)) |
| `Query(...)` parameters for filters | `FilterSet` and `Filter` ([filters over HTTP](queries.md#filters-over-http)) |
| `limit` and `offset` parameters | Keyset pages with cursors, `Pagination(max_limit=...)` ([pages](queries.md#pages)) |
| `raise HTTPException(404)` | A domain error (`NotFound`, `BusinessRuleViolation`), answered as JSON with a stable `code` ([problem codes](reference/problems.md)) |
| `RequestValidationError` and its `422` | The code `validation_error`, with `errors` for each field ([errors](reference/responses.md#errors)) |
| `exception_handler` | `ERROR_FORMAT`, and the error body every answer shares ([errors](reference/responses.md#errors)) |
| Versions in the URL | `versioned_router("v1")`, and `Deprecation` headers ([versions and deprecations](reference/responses.md#versions-and-deprecations)) |
| `UploadFile` | Uploads with limits, and storages ([files and uploads](uploads.md)) |
| `WebSocket`, `StreamingResponse` for events | WebSockets and server-sent events, with the caller's rules checked again while they stay open ([live connections](live.md)) |
| `ETag` headers of your own | Built in: each read has an `ETag`, and each change takes `If-Match` ([versions and conditional requests](resources.md#versions-and-conditional-requests)) |

## Users and security

| FastAPI | domainkit |
| --- | --- |
| `Depends(get_current_user)`, `Security(...)` | An access rule on every route: `Public`, `Require(...)`; `check` lists a route without one, and staging and production refuse to start ([protecting routes](auth/routes.md#protecting-routes)) |
| `OAuth2PasswordBearer`, `fastapi-users` | `domainkit.contrib.auth`: users, groups, permissions, tokens, JSON Web Tokens, and sessions in cookies ([users and permissions](auth.md)) |
| `python-jose`, `pyjwt` by hand | `JWTAuthentication`, with keys you can replace ([JSON Web Tokens](auth/authentication.md#json-web-tokens)) |
| OAuth 2 and OpenID Connect clients | `domainkit.contrib.identity` ([identity providers](auth/identity.md)) |
| `slowapi` | `RATE_LIMIT`, and throttles for one route ([throttles for one route](cache.md#throttles-for-one-route)) |
| Idempotency middleware of your own | `domainkit.contrib.idempotency` and the `Idempotency-Key` header ([idempotency](contrib/idempotency.md)) |

## Work after the response

| FastAPI | domainkit |
| --- | --- |
| `BackgroundTasks` | `uow.defer(...)`: the task is stored with the commit, and runs even if the process stops ([messaging](contrib/messaging.md)) |
| Celery, ARQ, Dramatiq | `@task`, `@periodic`, and `@inbox`, run by `runworker` from an outbox ([messaging](contrib/messaging.md)) |
| An event bus of your own | Domain events and `@handles` ([event handlers](runtime.md#event-handlers)) |
| `fastapi-mail` | `domainkit.contrib.mail`, sent through the outbox ([mail](mail.md)) |
| `fastapi-cache` | `domainkit.cache`, and whole-response caching ([caches](cache.md)) |

## Operations

| FastAPI | domainkit |
| --- | --- |
| `logging` set up by hand | `LOGGING`, with JSON lines and request ids ([observability](reference/observability.md)) |
| `prometheus-fastapi-instrumentator` | Metrics at `/metrics`, with the `metrics` extra ([observability](reference/observability.md)) |
| OpenTelemetry instrumentation | Traces, with the `tracing` extra ([observability](reference/observability.md)) |
| Timeouts and limits by hand | `REQUEST_TIMEOUT`, `MAX_CONCURRENT_REQUESTS`, and body limits ([server limits](reference/servers.md)) |
| Multi-tenancy by hand | `domainkit.tenancy` ([multi-tenancy](tenancy.md)) |

## Testing

| FastAPI | domainkit |
| --- | --- |
| `fastapi.testclient.TestClient` | `TestClient` from `domainkit.testing`, and the fixtures `client`, `admin_client`, and `client_for` ([testing](testing.md)) |
| `app.dependency_overrides` | Still works; tests also call use cases with an in-memory unit of work, `in_memory(...)`, and no HTTP ([testing](testing.md)) |
| A test database set up by hand | The plugin makes and migrates the test databases, and puts them back before each test ([test databases](testing.md#test-databases)) |
| `freezegun`, `factory_boy` | `freeze_time` and `Factory` ([factories](testing.md#factories)) |

## What has no FastAPI name

These ideas are new to a FastAPI user. Each has a page:

- aggregates and the rules they hold ([resources](resources.md));
- use cases and their commands ([use cases](resources.md#use-cases));
- users, groups, and model permissions ([users and permissions](auth.md));
- the transactional outbox ([messaging](contrib/messaging.md));
- the audit trail ([audit](contrib/audit.md));
- idempotency keys ([idempotency](contrib/idempotency.md)).

[The glossary](learning.md#words) explains each word, and
[resources](resources.md) is the guide to read first.
