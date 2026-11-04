# Coming from Django

domainkit keeps Django's conventions where they fit a domain-driven
service, and gives the rest names that say what they do. This page maps
what a Django or Django REST framework (DRF) user knows to its name in
domainkit. Each row links to the page that explains it.
[The tutorial](tutorial.md) shows the pieces working together.

On this page you find:

- the tables, one for each area of Django;
- the ideas that have no Django name;
- the Django features that domainkit leaves out on purpose.

## The biggest difference

In Django, the model is both the business object and the table. In
domainkit, these are two things:

- an [aggregate](learning.md#aggregate) is a plain Python class that
  holds the business rules, in `domain.py`;
- its table is made from its fields, in `models.py`
  ([tables from domain classes](databases.md#tables-from-domain-classes)).

Code changes data through [use cases](learning.md#use-case), each in one
[unit of work](learning.md#unit-of-work) (a transaction). The API, a
script, and a test all call the same use case, so all of them meet the
same rules.

## Projects, apps, and settings

| Django | domainkit |
| --- | --- |
| `django-admin startproject` | `domainkit init` ([tutorial, part 0](tutorial/0-first-route.md)) |
| `manage.py startapp` | `manage.py startdomain`: four files, `domain.py`, `controllers.py`, `models.py`, and `routes.py`; `--layered` for a package per layer ([generators](resources.md#generators)) |
| An app | A [domain](learning.md#domain) ([tutorial, part 1](tutorial/1-first-resource.md)) |
| `INSTALLED_APPS` | `INSTALLED_DOMAINS` ([settings](reference/settings.md)) |
| `AppConfig`, `AppConfig.ready()` | `DomainConfig`, `DomainConfig.ready()`; `requires` names the apps it cannot work without ([optional apps](contrib.md#what-each-app-needs)) |
| `django.setup()` | `domainkit.setup()` ([public API](api/core.md)) |
| `settings.py` for each environment, `django-environ` | `config/settings/{base,local,test,production}.py`, and `Env` from `domainkit.conf` ([configuration](operations.md#configuration)) |
| `DJANGO_SETTINGS_MODULE` | `<PREFIX>SETTINGS_MODULE`, such as `BANK_API_SETTINGS_MODULE` ([settings](reference/settings.md)) |
| `manage.py diffsettings` | The same command ([commands](reference/commands.md)) |
| `manage.py check`, `check --deploy`, `SILENCED_SYSTEM_CHECKS` | `check`, `check --deploy`, `SILENCED_CHECKS`; staging and production refuse to start with an error ([deployment checks](reference/checks.md)) |
| `checks.register()` | Checks of your own ([deployment checks](reference/checks.md)) |
| Management commands, `BaseCommand`, `call_command` | `BaseCommand` in a domain's `management/commands/` ([commands](reference/commands.md)) |
| `django.core.signing`, `SECRET_KEY_FALLBACKS` | `domainkit.signing`, `SECRET_KEY_FALLBACKS` ([signing](reference/signing.md)) |
| Middleware, `MIDDLEWARE` | The same setting, with domainkit's middleware in a fixed order ([middleware](reference/http.md#middleware)) |
| `ALLOWED_HOSTS`, `SECURE_*`, HSTS, `X_FRAME_OPTIONS` | The same settings ([security headers](reference/http.md#security-headers)) |
| `django-cors-headers` | `CORS_ALLOWED_ORIGINS` and the other `CORS_*` settings ([HTTP settings](reference/http.md#http)) |

## Models and fields

| Django | domainkit |
| --- | --- |
| `models.Model` | An aggregate (`AggregateRoot`), and its table from its fields (`table_for`) ([tables from domain classes](databases.md#tables-from-domain-classes)) |
| Abstract base models | Class inheritance; `AbstractUser` for users ([users](auth/users.md)) |
| `CharField`, `IntegerField`, `DateTimeField`, and the other basic fields | Python types in the field's type hint ([tables from domain classes](databases.md#tables-from-domain-classes)) |
| `DecimalField` for money | `Money`, an exact amount and its currency ([money](money.md)) |
| `EmailField`, `URLField`, `SlugField`, `GenericIPAddressField` | `Email`, `URL`, `Slug`, `IPAddress` ([validation](validation.md#ready-field-types)) |
| `max_length`, `MinValueValidator`, `RegexValidator` | The markers `MaxLength`, `MinLength`, `Range`, and `Pattern` ([validation](validation.md#markers)) |
| `max_digits`, `decimal_places` | The marker `Digits(12, 2)` ([tutorial, part 2](tutorial/2-rules.md)) |
| `unique=True`, `db_index=True` | The markers `Unique()` and `Indexed()` ([databases](databases.md#unique-values)) |
| `db_default`, `GeneratedField` | `DatabaseDefault(sql)` and `Generated(sql)` ([columns the database fills](databases.md#columns-the-database-fills)) |
| `choices`, `TextChoices`, `IntegerChoices`, `get_FOO_display()` | `TextChoices` and `IntegerChoices` from `domainkit.ddd`: `Status.PAID.label`, `Status.choices()` ([public API](api/data.md)) |
| `Meta.constraints`, `Meta.indexes`, `Meta.ordering` | `Meta.unique`, `Meta.checks`, `Meta.indexes`, and `Meta.ordering` on the aggregate ([checks](databases.md#checks)) |
| `clean()`, `full_clean()` | `clean()`, run when the object is made and before each commit ([rules across fields](validation.md#rules-across-fields-clean)) |
| `pre_save` | `before_save()` on the aggregate ([fields worked out before a commit](validation.md#fields-worked-out-before-a-commit-before_save)) |
| `ForeignKey` to a child, inlines | Child entities, stored and deleted with their aggregate ([child entities](databases.md#child-entities)) |
| `ForeignKey` to another model | `Reference[OtherId]`: the other aggregate's id; you load each aggregate on its own ([databases](databases.md)) |
| `OneToOneField` | `Annotated[Reference[XId], Unique()]` ([databases](databases.md)) |
| `ManyToManyField` | A list of child entities that each hold a `Reference` ([many-to-many](databases.md#many-to-many)) |
| `GenericForeignKey` | `ObjectRef`: one column that names any aggregate ([references to any aggregate](databases.md#references-to-any-aggregate)) |
| Soft delete (a package) | `SoftDeletable`, built in ([soft delete](queries.md#soft-delete)) |
| Encrypted fields (a package) | The marker `Encrypted`, with keys from a KMS ([keys](contrib/keys.md)) |
| `django.contrib.postgres.search` | `F("name").search(words)`, and search engines ([search](search.md)) |
| GeoDjango, `PointField` | `Point` fields, and `F(...).within(point, meters)` ([places](search.md#places)) |

## Queries

| Django | domainkit |
| --- | --- |
| `Model.objects` | A [repository](learning.md#repository), reached through the unit of work: `uow.accounts` ([querying a repository](queries.md#querying-a-repository)) |
| `filter()`, `exclude()`, `Q` with `&`, `\|`, `~` | `repository.filter(F("name") == ...)`; criteria combine with `&`, `\|`, `~`, in SQL and in memory alike ([querying a repository](queries.md#querying-a-repository)) |
| Lookups such as `__icontains`, `__in`, `__gte`, `__isnull` | The same lookups, as methods and operators on `F` ([queries](queries.md)) |
| `get`, `first`, `count`, `exists` | The same names ([queries](queries.md)) |
| `get_or_create`, `update_or_create`, `in_bulk`, `bulk_create` | `get_or_create`, `update_or_create`, `get_many`, `add_all` ([several aggregates at once](queries.md#several-aggregates-at-once)) |
| `aggregate(Count(...), Sum(...))` | `repository.aggregate(n=Count(), total=Sum("amount"))` ([counting and totals](queries.md#counting-and-totals)) |
| `values()`, `values_list()` | `repository.values(query, "id", "name")` ([queries](queries.md)) |
| `iterator()`, `explain()` | `stream(batch=...)`, `explain()` ([reading many](queries.md#reading-many)) |
| `order_by()`, slicing, `Paginator` | `Ordering`, and keyset pages with cursors ([pages](queries.md#pages)) |
| Custom managers and query methods | A `Specification`: a named business rule, for Python and for queries ([specifications](queries.md#specifications)) |
| `select_related`, `prefetch_related` | Not needed: an aggregate loads whole, children included ([databases](databases.md)) |
| `select_for_update()` | `get_for_update`, `get_many_for_update`, `RowLock`, and `uow.lock(name)` ([row locks](queries.md#row-locks)) |
| `raw()`, `connection.cursor()` | SQLAlchemy in a repository of your own ([databases](databases.md)) |
| `using()`, database routers, read replicas | `DATABASE_ROUTING`, `REPLICAS`, and units of work made with `readonly=True` ([reading from a replica](recipes/read-replica.md)) |

## Transactions and migrations

| Django | domainkit |
| --- | --- |
| `transaction.atomic()` | A unit of work: `async with uow: ...` then `await uow.commit()`; `@use_case` does both for you ([transactions](databases.md#transactions)) |
| Savepoints | `uow.savepoint()` ([savepoints](queries.md#savepoints)) |
| `transaction.on_commit()` | Domain events, published after the commit, and `uow.defer(...)` ([messaging](contrib/messaging.md)) |
| Running a transaction again after a serialization failure | `uow.run(..., retry=Retry(...))` ([a payment that runs again](recipes/payments.md)) |
| `makemigrations`, `migrate`, `showmigrations`, `squashmigrations` | The same commands, on Alembic ([migrations in a rollout](operations.md#migrations-in-a-rollout)) |
| `sqlmigrate app 0002` | `migrate 0002 --sql --from 0001`: one revision's SQL, and nothing runs ([commands](reference/commands.md)) |
| `RunPython`, `RunSQL` (data migrations) | `makemigrations --empty`, then fill in the revision ([data migrations](databases.md#data-migrations)) |
| `inspectdb` | The same command: it writes aggregates for an existing database's tables ([commands](reference/commands.md)) |
| `dumpdata`, `loaddata`, `flush` | The same commands ([fixtures, flush, and seeds](reference/commands.md#data-fixtures-flush-and-seeds)) |
| `dbshell`, `shell` | The same commands; `shell` is an asyncio console where `await` works ([commands](reference/commands.md)) |

## The API (Django REST framework)

| Django REST framework | domainkit |
| --- | --- |
| `ViewSet` and a router | `ResourceRouter`: the reads, and one route for each use case ([resources](resources.md)) |
| `@action(detail=True)` | `ResourceRouter.use_case(..., path="/deposit")` ([use cases](resources.md#use-cases)) |
| `ModelSerializer` | `schema_for(Model)` ([schemas from the domain](resources.md#schemas-from-the-domain)) |
| `validate_<field>()`, `validate()` | Markers on the fields, and `clean()` on the aggregate ([validation](validation.md)) |
| `partial_update` (`PATCH`) | `PatchModel` and partial changes ([partial changes](resources.md#partial-changes)) |
| `django-filter`, `OrderingFilter`, `SearchFilter` | `FilterSet` and `Filter` ([filters over HTTP](queries.md#filters-over-http)) |
| Pagination classes | Keyset pages with cursors, `Pagination(max_limit=...)` ([pages](queries.md#pages)) |
| Permission classes, `DjangoModelPermissions` | `Require(...)`, `ModelActions`, and `HasPermissions`; every route says who may call it ([protecting routes](auth/routes.md#protecting-routes)) |
| `django-guardian` (object permissions) | `domainkit.contrib.objectpermissions` ([grants on single objects](auth/users.md#grants-on-single-objects)) |
| Throttling | `RATE_LIMIT`, and throttles for one route ([throttles for one route](cache.md#throttles-for-one-route)) |
| `ETag`, `If-Match`, `If-None-Match` | Built in: each read has an `ETag`, and each change takes `If-Match` ([versions and conditional requests](resources.md#versions-and-conditional-requests)) |
| API versioning | `versioned_router("v1")`, and `Deprecation` headers ([versions and deprecations](reference/responses.md#versions-and-deprecations)) |
| Exception handling, `APIException` | Domain errors (`NotFound`, `BusinessRuleViolation`), answered with a stable `code` ([problem codes](reference/problems.md)) |
| The browsable API, OpenAPI | OpenAPI at `/openapi.json`, with pages at `/docs` and `/redoc`; `manage.py openapi` ([HTTP settings](reference/http.md#http)) |
| The admin site | An admin API over REST, for users and for any aggregate; no HTML ([the generic admin](resources.md#the-generic-admin)) |

## Users and security

| Django | domainkit |
| --- | --- |
| `django.contrib.auth`, `createsuperuser`, `changepassword` | `domainkit.contrib.auth`, and the same commands ([users and permissions](auth.md)) |
| `django.contrib.contenttypes` | `domainkit.contrib.contenttypes` ([installing auth](auth/install.md)) |
| A custom user model, `AUTH_USER_MODEL` | `startdomain accounts --user`, before the first `migrate` ([a custom user model](auth/users.md#a-custom-user-model)) |
| Password hashers and validators | The same design, in the `AUTH` setting ([passwords](auth/users.md#passwords)) |
| Authentication backends | Authentication classes and backends ([authenticating requests](auth/authentication.md)) |
| Sessions, `login()`, `logout()` | Opaque tokens, JSON Web Tokens, and sessions in cookies ([logging in](auth/logging-in.md)) |
| Password reset, email verification | Built in ([password reset](auth/logging-in.md#password-reset)) |
| `login_required`, `permission_required` | `Require(IsAuthenticated())`, `Require(HasPermissions(...))` ([protecting routes](auth/routes.md#protecting-routes)) |
| `django-otp`, `django-two-factor-auth` | Second factors and recovery codes, built in ([two-factor logins](auth/logging-in.md#two-factor-logins)) |
| `django-allauth`, social logins | `domainkit.contrib.identity`: OpenID Connect, OAuth 2, SAML, and LDAP ([identity providers](auth/identity.md)) |
| CSRF protection | Built in for sessions in cookies ([browser sessions](auth/authentication.md#browser-sessions-in-cookies)) |

## Work after the response

| Django | domainkit |
| --- | --- |
| Signals (`post_save`, `post_delete`) | Domain events, published after the commit; `durable=True` runs them in the worker ([messaging](contrib/messaging.md)) |
| `@receiver` | `@handles(Event, durable=...)` in a domain's `controllers.py` ([event handlers](runtime.md#event-handlers)) |
| Celery tasks and beat | `@task`, `uow.defer(...)`, and `@periodic`, run by `runworker` from an outbox ([messaging](contrib/messaging.md)) |
| Celery's `acks_late` and idempotent tasks | `@inbox`: the task's database work commits once for each message ([work that commits once per message](recipes/exactly-once.md)) |
| Celery's broker | The outbox table, or a broker such as RabbitMQ ([other brokers](contrib/messaging.md#other-brokers)) |

## Mail, files, languages, and caches

| Django | domainkit |
| --- | --- |
| `send_mail`, `EmailMessage`, email backends | `domainkit.contrib.mail`, sent through the outbox ([mail](mail.md)) |
| `mail_admins`, error emails | `mail_admins()`, and `report_to_admins` in `ERROR_REPORTERS` ([telling the admins](mail.md#telling-the-admins)) |
| `gettext`, `makemessages`, `compilemessages`, `LANGUAGE_CODE`, `TIME_ZONE` | `domainkit.i18n`, the same commands and settings ([languages](i18n.md)) |
| Storage backends, `FileField` | `domainkit.storage`, and uploads ([files and uploads](uploads.md)) |
| The cache framework, `cache_page` | `domainkit.cache`, and whole-response caching ([caches](cache.md)) |
| Channels, WebSockets | WebSockets and server-sent events, built in ([live connections](live.md)) |

## Testing

| Django | domainkit |
| --- | --- |
| `TestCase`, `self.client` | pytest fixtures: `client`, `admin_client`, and `client_for` ([testing](testing.md)) |
| `override_settings` | `override_settings` from `domainkit.conf`, and the `settings` fixture ([testing](testing.md)) |
| `assertNumQueries` | `assert_num_queries` ([counting queries](testing.md#counting-queries)) |
| `factory_boy`, `freezegun` | `Factory` and `freeze_time` ([factories](testing.md#factories)) |
| `mail.outbox`, `captureOnCommitCallbacks` | The `outbox` fixture and the memory mail backend ([testing](testing.md)) |
| `LiveServerTestCase`; pytest-django's `live_server` and `django_db_blocker` | `live_server` and `database_blocker` ([a live server](testing.md#a-live-server)) |
| `RequestFactory` | Not needed: call a use case with an in-memory unit of work, `in_memory(...)` ([testing](testing.md)) |

## What has no Django name

These ideas are new to a Django user. Each has a page:

- aggregates and the rules they hold ([resources](resources.md));
- commands and use cases ([resources](resources.md#use-cases));
- the transactional outbox and the inbox ([messaging](contrib/messaging.md));
- the audit trail and its seals ([audit](contrib/audit.md));
- evidence records on a ledger ([evidence](contrib/evidence.md));
- idempotency keys ([idempotency](contrib/idempotency.md));
- exact money ([money](money.md)).

## What domainkit leaves out

domainkit is an API framework, and answers JSON. It has no HTML
templates, forms, static files, messages framework, sites, flatpages,
sitemaps, or feeds. It has no multi-table or proxy inheritance: one
aggregate has one table.

[The feature inventory](feature-inventory.md) lists every Django
feature, what domainkit has for it, and the step that adds what it
lacks.
