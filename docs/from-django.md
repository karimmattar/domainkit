# Coming from Django

domainkit keeps Django's conventions where they fit a domain-driven
service, and names the rest after what they do. This page maps what a
Django or Django REST framework user reaches for to its name here, and
the page that says how. [The tutorial](tutorial.md) shows them working
together.

## Projects, apps, and settings

| Django | domainkit |
| --- | --- |
| `django-admin startproject` | `domainkit init` ([tutorial](tutorial/1-first-resource.md)) |
| `manage.py startapp` | `manage.py startdomain`, a layered package; `--minimal` for a small one ([generators](resources.md#generators)) |
| `INSTALLED_APPS`, `AppConfig.ready()` | `INSTALLED_DOMAINS`, `DomainConfig.ready()` ([api.md](api.md)) |
| `settings.py` per environment, `django-environ` | `config/settings/{base,local,test,production}.py`, `domainkit.conf.Env` ([operations.md](operations.md#configuration)) |
| `manage.py check --deploy` | `manage.py check --deploy`: errors and warnings with ids, `SILENCED_CHECKS` ([deployment checks](reference.md#deployment-checks)) |
| `django.core.signing`, `SECRET_KEY_FALLBACKS` | `domainkit.signing`, `SECRET_KEY_FALLBACKS` ([security.md](security.md)) |

## Models and queries

| Django | domainkit |
| --- | --- |
| `models.Model` | An aggregate (`AggregateRoot`), plain Python, and its table from its fields (`table_for`) ([databases.md](databases.md#tables-from-domain-classes)) |
| `ForeignKey` to a child, inlines | Child entities, stored and deleted with their aggregate ([databases.md](databases.md#child-entities)) |
| `ForeignKey` to another model | A `Reference` by id: aggregates load one at a time |
| `Model.objects.filter(...)` | `repository.filter(F("name") == ...)`, the same in SQL and in memory ([queries.md](queries.md#querying-a-repository)) |
| `makemigrations`, `migrate`, `squashmigrations` | The same commands, on Alembic ([operations.md](operations.md#migrations-in-a-rollout)) |
| `transaction.atomic()` | A unit of work: `async with uow: ... await uow.commit()` ([databases.md](databases.md#transactions)) |
| A savepoint | `uow.savepoint()` ([queries.md](queries.md#savepoints)) |
| `select_for_update()` | `get_for_update`, `get_many_for_update`, `uow.lock(name)` ([payments recipe](recipes/payments.md)) |
| Database routers, read replicas | `DATABASE_ROUTING`, `REPLICAS`, `readonly=True` ([read replica recipe](recipes/read-replica.md)) |
| Soft delete (packages) | Built in ([queries.md](queries.md#soft-delete)) |
| `dumpdata`, `loaddata`, `flush` | The same commands ([reference.md](reference.md)) |

## The API

| Django REST framework | domainkit |
| --- | --- |
| `ViewSet` and a router | `ResourceRouter`: reads, and one route per command ([resources.md](resources.md)) |
| `ModelSerializer` | `schema_for(Model)` ([resources.md](resources.md#schemas-from-the-domain)) |
| `django-filter`, ordering, search | `FilterSet` and `Filter` ([queries.md](queries.md#filters-over-http)) |
| Pagination classes | Keyset pages with cursors, `Pagination(max_limit=...)` ([queries.md](queries.md#pages)) |
| Permission classes, `DjangoModelPermissions` | `Require(...)`, `ModelActions`, `HasPermissions`; every route says who may call it ([auth](auth/routes.md#protecting-routes)) |
| `django-guardian` | `domainkit.contrib.objectpermissions`: grants on single objects ([auth](auth/users.md#grants-on-single-objects)) |
| Throttling | `RATE_LIMIT`, and throttles for one route ([cache.md](cache.md#throttles-for-one-route)) |
| `ETag`s, `If-Match` | Built in: every read has an `ETag`, every change takes `If-Match` ([resources.md](resources.md#versions-and-conditional-requests)) |
| The admin site | An admin API for users and for any aggregate ([resources.md](resources.md#the-generic-admin)) |

## Users, work after the response, and the rest

| Django | domainkit |
| --- | --- |
| `django.contrib.auth`, `createsuperuser` | `domainkit.contrib.auth`, the same command; tokens, JWTs, cookie sessions ([auth.md](auth.md)) |
| Signals (`post_save`) | Domain events, published after the commit; `durable=True` runs them in the worker ([messaging](contrib/messaging.md)) |
| Celery tasks and beat | `@task`, `uow.defer(...)`, `@periodic`, run by `runworker` from an outbox ([messaging](contrib/messaging.md)) |
| The cache framework, `cache_page` | `domainkit.cache`, and whole-response caching ([cache.md](cache.md)) |
| `send_mail`, email templates | `domainkit.contrib.mail`, through the outbox ([mail.md](mail.md)) |
| `gettext`, `LANGUAGE_CODE`, `TIME_ZONE` | `domainkit.i18n`, the same settings ([i18n.md](i18n.md)) |
| Storage backends | `domainkit.storage` ([uploads.md](uploads.md)) |
| `TestCase`, `self.client`, `override_settings` | pytest fixtures: `client`, `admin_client`, `client_for`; `override_settings` from `domainkit.conf` ([testing.md](testing.md)) |
| `assertNumQueries`, `factory_boy`, `freezegun` | `assert_num_queries`, `Factory`, `freeze_time` ([testing.md](testing.md)) |

What has no Django name: aggregates and their rules, commands, the
transactional outbox, the inbox, the audit trail and its seals, evidence
records, and idempotency keys. [resources.md](resources.md) and
[the optional apps](contrib.md) start there.
