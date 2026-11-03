# Feature inventory

What an application needs from a framework, Django's features first and
then the platform around them, checked against domainkit's code as of
1.5. Each row says what exists and which step of the
[roadmap](roadmap.md) closes the gap. A step named **C** is part of the
core revamp, which comes first; one named **A** is an adapter, built on
the core once it is done.

Status: **Yes** is there and documented; **Partial** is there with a gap
the row names; **No** is missing; **n/a** does not fit a domain-driven API
framework, for the reason given.

## A. Django and Django REST framework

### Models and fields

| Django | domainkit | Status | Step |
| --- | --- | --- | --- |
| `models.Model` | An aggregate (`AggregateRoot`), its table from its fields (`table_for`) | Yes | |
| Abstract base models | Dataclass inheritance; `AbstractUser` | Yes | |
| Multi-table and proxy inheritance | One aggregate, one table; a role is a field | n/a: aggregates are not class hierarchies over tables | |
| Char, Text, Integer, Decimal, Boolean, Date, DateTime, Duration, UUID, JSON fields | Python types in the field's hint | Yes | |
| `EmailField`, `URLField`, `SlugField`, `GenericIPAddressField` | `Email`, `URL`, `Slug`, `IPAddress` (`domainkit.ddd`), checked by the object, the request, and OpenAPI ([validation.md](validation.md)) | Yes | C3 |
| `choices` | An `Enum` field | Yes | |
| `get_FOO_display()`, choice labels | `TextChoices` and `IntegerChoices`: `member.label`, `choices()` | Yes | C7 |
| `null`, `default` | `X \| None`, dataclass defaults | Yes | |
| `db_default`, `GeneratedField` | A hand-written column in `table_for(..., *extra)` | Partial | C7 |
| `unique=True`, `db_index=True` | `Unique()` and `Indexed()` on the field | Yes | C7 |
| `Meta.constraints` (`UniqueConstraint`, `CheckConstraint`), `Meta.indexes` | `Meta.unique`, `Meta.indexes`, and `Meta.checks` (criteria, held at commit and by the database) on the aggregate | Yes | C7 |
| `Meta.ordering` | `Meta.ordering` on the aggregate: queries that name none follow it, then the id | Yes | C7 |
| Validators (`MaxLengthValidator`, `RegexValidator`, `MinValueValidator`, ...) | `MaxLength`, `MinLength`, `Pattern`, `Range` markers, checked by the object, the request, and OpenAPI ([validation.md](validation.md)) | Yes | |
| `clean()`, `full_clean()`, `clean_fields()` | `clean()`, run when made and before every commit; fields checked whenever set | Yes | |
| `ForeignKey` to a child | Child entities, stored with their aggregate | Yes | |
| `ForeignKey` to another model | `Reference[OtherId]` by id | Yes | |
| `OneToOneField` | `Annotated[Reference[XId], Unique()]` | Yes | C7 |
| `ManyToManyField` | A list of child entities holding a `Reference`: their table is the link table ([databases.md](databases.md#many-to-many)) | Yes | C7 |
| `GenericForeignKey` | `contrib.contenttypes` ids | Partial | |
| Signals (`post_save`, `post_delete`) | Domain events, published after the commit | Yes | |
| `pre_save` | `on_stage` hooks | Partial | |

### Queries

| Django | domainkit | Status | Step |
| --- | --- | --- | --- |
| `filter`, `exclude`, `Q` with `&`, `\|`, `~` | `repository.filter(F(...) == ...)`, criteria combine with `&`, `\|`, `~` | Yes | |
| `get`, `first`, `count`, `exists` | The same names | Yes | |
| `order_by`, slicing | `Ordering`, keyset pages, `limit` and `offset` | Yes | |
| Lookups: `exact`, `iexact`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, `in`, `range`, `gt`, `gte`, `lt`, `lte`, `isnull` | The same, on `F` | Yes | |
| `regex`, `iregex` | `F(...).regex(p)`, `.iregex(p)`, in code; not offered over HTTP | Yes | C7 |
| Date transforms (`__year`, `__month`, `__date`) | `F(...).year(y)`, `.month(y, m)`, `.on(day)`: ranges in UTC, on date and datetime fields | Yes | C7 |
| `get_or_create`, `update_or_create` | The same names, the aggregate's factory and methods doing the work; a lost insert race answers the winner | Yes | C7 |
| `bulk_create` | `add_all` | Yes | |
| `in_bulk` | `get_many`, and `get_many_for_update` to lock | Yes | C7 |
| `update()` and `delete()` on a queryset | `remove_all` of loaded aggregates | Partial: no set-based write, on purpose for aggregates; offered for reference data | C7 |
| `aggregate()` (`Count`, `Sum`, `Avg`, `Min`, `Max`) | `repository.aggregate(criteria, name=Sum(...))`, one statement | Yes | C7 |
| `annotate()`, `values()`, `values_list()`, `only()`, `defer()` | `repository.values(query, "id", "name")`: dicts of the fields, one statement, nothing loaded; `aggregate()` for measures | Partial: no `annotate()` per row | C4 |
| `select_related`, `prefetch_related` | An aggregate loads whole, children included | n/a | |
| `select_for_update(nowait, skip_locked)` | `get_for_update`, `RowLock` | Yes | |
| `iterator()`, `aiterator()` | `stream(query, batch=...)`, keyset batches ([queries.md](queries.md)) | Yes | C6 |
| `F()` expressions in updates | `update_rows(..., stock=F("stock") - 1)`: `+`, `-`, `*` computed in SQL | Yes | C7 |
| `Case`, `When`, `Subquery`, `Window`, database functions | SQLAlchemy inside a repository of the project's own | Partial: by hand | |
| `raw()`, `connection.cursor()` | The session of a SQL repository | Partial: by hand | |
| `explain()` | `explain(query, analyze=...)` on SQL repositories | Yes | C6 |
| `using()`, routers, replicas | `database`, `DATABASE_ROUTING`, `REPLICAS`, `readonly=True` | Yes | |

### Transactions and migrations

| Django | domainkit | Status | Step |
| --- | --- | --- | --- |
| `transaction.atomic()`, `durable=True` | A unit of work; `@use_case` opens and commits it | Yes | |
| `transaction.on_commit()` | Events after the commit, `uow.defer(...)` | Yes | |
| Savepoints | `uow.savepoint()` | Yes | |
| `makemigrations`, `migrate`, `showmigrations`, `squashmigrations` | The same commands, on Alembic | Yes | |
| `sqlmigrate` | `migrate REVISION --sql --from PREVIOUS`, rendered offline by Alembic | Yes | C7 |
| Data migrations (`RunPython`, `RunSQL`) | Alembic operations, by hand | Partial | |
| `inspectdb` | The same command: aggregates and `table_for` calls printed from the tables, to start from | Yes | C7 |
| `dumpdata`, `loaddata`, `flush`, `dbshell`, `shell`, `diffsettings` | The same commands | Yes | |

### Requests, the API, and security

| Django and DRF | domainkit | Status | Step |
| --- | --- | --- | --- |
| URLconf, `include`, path converters, `reverse()` | FastAPI routes, typed path parameters, `url_path_for` | Yes | |
| `ViewSet`, router, `@action` | `ResourceRouter`, `use_case` and `command_route` | Yes | |
| `ModelSerializer`, nested serializers | `schema_for`, child entities included | Yes | |
| `validate_<field>()`, `validate()` | Field markers, and `clean()` on the aggregate | Yes | |
| Generic views (List, Retrieve, Create, Update, Destroy) | Reads by default, `crud=True` for reference data | Yes | |
| Pagination | Keyset cursors; offset through `limit` and `offset` | Yes | |
| Filtering, search, ordering (`django-filter`) | `FilterSet`, `Filter` | Yes | |
| Permissions, `login_required`, `permission_required` | `Require`, `ModelActions`, deny by default | Yes | |
| Object permissions (`django-guardian`) | `contrib.objectpermissions` | Yes | |
| Throttling | `RATE_LIMIT`, per-route throttles | Yes | |
| Renderers, content negotiation | `FORMATS` | Yes | |
| API versioning | Prefixes, `Deprecation` headers | Partial | |
| OpenAPI, browsable API | OpenAPI and its docs pages | Yes | |
| `ETag`, `If-Match`, `If-None-Match` | Built in | Yes | |
| `Last-Modified`, `If-Modified-Since` | `ResourceRouter(..., last_modified="updated_at")` | Yes | C6 |
| `cache_page`, low-level cache | `domainkit.cache`, page caching | Yes | |
| `GZipMiddleware` | `domainkit.http.compression.GZipMiddleware`, set by `GZIP_MINIMUM_SIZE` and `GZIP_LEVEL` | Yes | C6 |
| `StreamingHttpResponse`, `FileResponse` | Streaming responses; files through `domainkit.storage` | Yes | |
| CSRF, `SECURE_*`, HSTS, CSP, clickjacking | Built-in middleware and settings | Yes | |
| CORS (`django-cors-headers`) | Built-in middleware | Yes | |
| Sessions | Cookie sessions in `contrib.auth` | Yes | |
| Templates, forms, static files, the messages framework, sites, flatpages, sitemaps, syndication | None | n/a: an API framework renders JSON; mail has its own templates | |

### Users, work after the response, and tooling

| Django | domainkit | Status | Step |
| --- | --- | --- | --- |
| Users, groups, permissions, custom user model | `contrib.auth`, `startdomain accounts --user` | Yes | |
| Password hashers, validators, upgrade on login | Yes, the same design | Yes | |
| Password reset, email verification | `contrib.auth.recovery` | Yes | |
| `createsuperuser`, `changepassword` | The same commands | Yes | |
| Authentication backends | Authentication classes and backends | Yes | |
| The admin site (HTML) | An admin API, actions, the audit trail as `LogEntry` | Partial: no HTML interface | |
| Celery tasks and beat | `@task`, `uow.defer`, `@periodic`, `runworker` | Yes | |
| `send_mail`, backends (SMTP, console, memory) | `contrib.mail` | Yes | |
| `mail_admins`, error emails | `domainkit.reporting` (Sentry) | Partial | |
| `gettext`, `compilemessages`, time zones | `domainkit.i18n`, `compilemessages` | Yes | |
| `makemessages` | The same command: messages found with `ast`, translations kept, unused ones obsolete | Yes | C7 |
| Custom management commands, `call_command` | `BaseCommand`, the same shape | Yes | |
| System checks, `register()` | `check`, `--deploy`, custom checks | Yes | |
| `TestCase`, `Client`, `override_settings`, `assertNumQueries`, `LiveServerTestCase` | pytest fixtures, `override_settings`, `assert_num_queries`, `live_server` | Yes | |
| `mail.outbox`, `captureOnCommitCallbacks` | `MemoryBackend`, the `outbox` fixture | Yes | |
| `RequestFactory` | Use cases are plain functions, called in tests with a unit of work; routes through `client_for` | n/a: there is no view to hand a request to | C7 |
| `factory_boy`, `freezegun` | `Factory`, `freeze_time` | Yes | |

## B. The platform around the application

| Need | domainkit | Status | Step |
| --- | --- | --- | --- |
| File storage on disk, in memory for tests | `FileSystemStorage`, `MemoryStorage` | Yes | |
| Object storage: S3 and S3-compatible (AWS S3, Huawei OBS, MinIO, Cloudflare R2, Alibaba OSS), Google Cloud Storage, Azure Blob | `S3Storage`, `GCSStorage`, `AzureBlobStorage`: multipart writes, ranged reads, no SDK ([uploads.md](uploads.md#object-storage)) | Yes | A1 |
| Presigned upload and download URLs | `presigned_url`, `presigned_upload` (size and type signed on S3 and GCS) | Yes | A1 |
| Image thumbnails, virus scanning of uploads | `thumbnail` (Pillow, bombs refused); `UPLOAD_SCANNER` with ClamAV, failing closed | Yes | A1 |
| Login with an identity provider: OpenID Connect and OAuth 2 (Google, Microsoft, Apple, GitHub, Keycloak, Okta, Auth0, AWS Cognito, Huawei IAM) | `domainkit.contrib.identity`: PKCE, state, nonce, ID tokens verified against the provider's keys ([identity.md](auth/identity.md)) | Yes | A2 |
| Enterprise single sign-on: SAML 2.0, LDAP and Active Directory | SAML 2.0 service provider (signed responses only, one-time tickets); `LDAPBackend` with group mapping | Yes | A2 |
| Linking several providers to one user, signing up through one | `/identity/{name}/link`, `/identity/linked`; `IDENTITY.SIGNUP` for verified emails no account has | Yes | A2 |
| Second factors: TOTP, recovery codes, step-up | `contrib.auth` (`mfa`, `stepup`, `sca`) | Yes | |
| Passkeys (WebAuthn) | None | No | A3 |
| Device binding: register a device's key, sign requests with it, list and revoke devices, trusted devices for step-up | Device proofs for login throttling only | Partial | A3 |
| Push tokens per device | `PushBackend` port, memory adapter | Partial: no provider | A4 |
| Key management: local keys, AWS KMS, HashiCorp Vault | `contrib.keys`, envelope encryption, rotation, re-encryption | Yes | |
| Key management: Huawei Cloud KMS, Google Cloud KMS, Azure Key Vault, an HSM through PKCS#11 | None | No: a provider interface first | A5 |
| SMS providers (Twilio, Vonage, AWS SNS, Unifonic, Huawei SMN) | `SmsBackend` port, console and memory adapters | Partial | A4 |
| Push providers (Firebase FCM, APNs, Huawei Push Kit) | `PushBackend` port, memory adapter | Partial | A4 |
| Email providers by API (AWS SES, SendGrid, Mailgun, Postmark) | SMTP | Partial | A4 |
| Message brokers: RabbitMQ; Kafka, NATS, Redis Streams, AWS SQS | `RabbitMQBroker`, memory | Partial | A6 |
| Cache: Redis, memory | `domainkit.cache` | Yes | |
| Tracing (OpenTelemetry), metrics (Prometheus), errors (Sentry), structured logs | Built in | Yes | |
| Health and readiness probes | Built in | Yes | |
| Outgoing HTTP with retries, timeouts, circuit breaker | `domainkit.http.client`, `contrib.webhooks` | Yes | |
| Multi-tenancy: a tenant per request, rows scoped to it, a database or schema per tenant | Sharding by key; caches and logs tagged | Partial | A7 |
| Full-text search: PostgreSQL search, OpenSearch, Meilisearch | None | No | A8 |
| Geospatial (PostGIS) | None | No | A8 |
| Captcha (Turnstile, reCAPTCHA) on sign-up and login | `AUTH.CAPTCHA`: Turnstile, reCAPTCHA, hCaptcha on login, signup, password reset, failing closed | Yes | A2 |
| Impersonation by support staff, audited | `POST /admin/auth/users/{id}/impersonate` with a reason, `AUTH.IMPERSONATION_TTL`; audited, no step-up | Yes | A2 |
| Feature flags, webhooks, approvals, audit trail, privacy (erasure, export), limits, idempotency | `contrib.flags`, `webhooks`, `approvals`, `audit`, `privacy`, `limits`, `idempotency` | Yes | |
| WebSockets and server-sent events | Built in, with the request's user and limits | Yes | |
| GraphQL, gRPC | None | n/a for now: REST first | |
