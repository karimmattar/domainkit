# Roadmap: an easy domainkit

What building a first application on 1.0 showed was hard, and the plan to
make it easy, in the order it pays off. Each step keeps the rules that make
domainkit safe (layers, deny by default, exact money) and removes ceremony
around them.

## Done in 1.5

| Need | What 1.5 gives |
| --- | --- |
| A domain is many packages and files | `startdomain` writes four files, one per layer: `domain.py`, `controllers.py`, `models.py`, `routes.py`. `--layered` keeps packages for large domains. |
| A custom user model takes six manual steps, and the docs put it where `check` refuses it | `startdomain accounts --user` writes it, installs it, and fills in `AUTH`; the test settings keep it. |
| A use case is a command class, a service, a schema, and a route line | `@use_case` on one async function; `ResourceRouter.use_case` makes its route. |
| One aggregate's storage is eight hand-written pieces | The unit of work's annotations: SQL repositories and `in_memory(...)` for tests are made from them. |
| Only `MaxLength` limits text, and only at the API | `MinLength`, `Pattern`, and `Range`; every marker checked by the object itself, by request bodies, and in OpenAPI; `clean()` for rules across fields. |

## First: revamp the core

Building an application on 1.0 showed the core is hard to follow before it
is hard to extend. Before more features, each part of the core gets one
obvious way to do each task. The steps run in this order, one at a time;
a step is done when it has a design note here, tests covering its new code
at 98% or more, a freshly generated project passing its tests and mypy,
and one pushed commit.

| # | Step | What changes | Status |
| --- | --- | --- | --- |
| C1 | A use case is one function | `@use_case` makes the command, the request body, and the transaction from one async function; `ResourceRouter.use_case` its route. | Done |
| C2 | Storage is declared | The unit of work's annotations make the SQL repositories and `in_memory(...)` for tests. | Done |
| C3 | One validation path | Field markers (`MaxLength`, `MinLength`, `Pattern`, `Range`) checked by the object itself whenever a field is set (aggregates, entities, value objects, commands); `clean()` for rules across fields, run when the object is made and on each aggregate before every commit; a refusal is a 422 naming the field. Loading from the database checks nothing ([validation.md](validation.md)). | Done |
| C4 | One request flow | Route, command, use case, aggregate, commit, events after commit, outbox: written down and drawn, with what each stage costs ([request-flow.md](request-flow.md)). Measured: a command makes one SQL statement in one transaction, and domainkit's routing 39 of its 3,405 calls; the largest cost is FastAPI resolving the endpoint's parameters on every request, which C6 moves to startup. | Done |
| C5 | Structure | One shape per kind of thing: every command of several actions is an `ActionCommand` (`manage.py <command> <action>`, `--operator` only on the actions that change data, DSGN-30); one home per name, the 1.0 aliases removed (DSGN-29); every refusal of a deployment has a check id (DSGN-32). | Done |
| C6 | Speed | Budgets on the benchmark ladder for a read, a write, and a list page; per-request work that does not depend on the request moved to startup. A resource route reads its conditional headers itself, 255 calls fewer a command (3,371 to 3,116) and 235 a read; a list page has its rung and budget (`resource-list`, one statement); `settings` keeps a value once read and a unit of work finds its domain once, about 30 calls fewer a request (a command 3,083, a read 2,359, a list page 3,025); `stream` reads a large query in keyset batches, and `explain` gives the database's plan. | Done |
| C7 | Django's conveniences | `get_or_create`, `update_or_create`, `get_many`; `aggregate` with `Count`, `Sum`, `Avg`, `Min`, `Max`; `stream` and `explain` (C6); `TextChoices` and `IntegerChoices` with labels; `Unique()`, `Indexed()`, `Meta.unique` and `Meta.indexes`; `regex` and `iregex`; `year`, `month`, and `on` periods; `F()` in `update_rows`; `makemessages` and `inspectdb`; many-to-many as child entities holding references ([feature-inventory.md](feature-inventory.md)). | Done |
| C8 | Proof | The guide's shop rebuilt on the new core; its lines of code, before and after, recorded here ([the proof](#the-proof)). | Done |

Steps C1 to C8 are the core's; the [feature inventory](feature-inventory.md)
names each Django feature and platform need, what exists, and the step
that closes its gap.

### The proof

The guide's shop, one `Product` aggregate with four commands (publish,
reprice, restock, discontinue), made by the generators twice: as 1.0
laid a domain out (packages for the domain, the application, the
infrastructure, and the API) and as a four-file domain. Both pass their
own tests; lines of code count neither blank lines, comments, nor
docstrings. `domainkit_tests/test_proof.py` makes and measures both, and
fails if these figures drift:

| Layout | Files | Lines of code | Lines a command adds |
| --- | --- | --- | --- |
| layered | 10 | 371 | 25 |
| four-file | 4 | 131 | 13 |

The four-file shop is about a third the size: a command is one method,
one `@use_case` function, and one route line, with no command class,
service, schema, repository port, or mapper to write.

After C7, the core closed the inventory's last open rows of its own: the
`Email`, `URL`, `Slug`, and `IPAddress` field types; `Meta.ordering` and
`Meta.checks`; `GZipMiddleware` and `Last-Modified`; and `values()`
projections.

## Then: adapters on the core

Each adapter is a port in the core (the shape every provider fills) and
its providers behind settings, with an in-memory one for tests, built
after the core steps above. All eight are done (#102):

| # | Adapter | Providers |
| --- | --- | --- |
| A1 | Object storage | S3 and S3-compatible (AWS S3, Huawei OBS, MinIO, Cloudflare R2, Alibaba OSS), Google Cloud Storage, Azure Blob; presigned URLs; thumbnails and virus scanning as upload hooks |
| A2 | Identity providers | OpenID Connect and OAuth 2 login (Google, Microsoft, Apple, GitHub, Keycloak, Okta, Auth0, Cognito, Huawei IAM), SAML 2.0, LDAP; providers linked to one user; captcha; audited impersonation |
| A3 | Devices | Passkeys (WebAuthn); device binding: a device's key registered, requests signed with it, devices listed and revoked, trusted for step-up |
| A4 | Notification providers | SMS (Twilio, Vonage, SNS, Unifonic, Huawei SMN), push (FCM, APNs, Huawei Push Kit), email by API (SES, SendGrid, Mailgun, Postmark) |
| A5 | Key management | A provider interface for `contrib.keys`; Huawei Cloud KMS, Google Cloud KMS, Azure Key Vault, PKCS#11 HSMs |
| A6 | Brokers | Kafka, NATS, Redis Streams, SQS beside RabbitMQ |
| A7 | Multi-tenancy | A tenant per request; rows, caches, and limits scoped to it; a database or schema per tenant |
| A8 | Search and places | PostgreSQL full-text search, OpenSearch, Meilisearch; PostGIS fields and lookups |

### What the core asks of an application today

Measured on the guide's shop, one aggregate with four commands:

| To add | An application writes today | Where |
| --- | --- | --- |
| One use case | A `Command` class, a service function, a request schema (`schema_for`), and a `command_route` line | four places, two files |
| One aggregate's storage | A repository port, a unit-of-work port, a SQLAlchemy repository, a SQLAlchemy unit of work, `table_for`, `map_imperatively`, `start_mappers`, and an in-memory fake for tests | eight pieces |
| One validated field | A marker for the API (`MaxLength`), a value object for the domain, and, on a user, `create` and `clean_field` overrides | three mechanisms |
| A custom user field | The class, a hand-written column, `EDITABLE_FIELDS`, and conftest overrides of `admin_client` and `client_for` | four places |

### The target design

- **A use case is one function** (done in 1.5). `@use_case` on `async def
  restock(uow, product_id: UUID, quantity: Annotated[int, Range(min=1)]) ->
  Product` is enough: the command, its request schema, its route, its
  OpenAPI, and its permission (`change_product`) are derived from the
  signature, and the unit of work is opened and committed around it.
  `Command` classes stay for messages that travel (tasks, approvals).
- **An aggregate's storage is declared, not written** (done in 1.5). The
  unit of work's annotations (`products: ProductRepository`) make the SQL
  repositories and an in-memory twin for tests (`in_memory`); a repository
  class of its own is written only when the port adds queries. Tables stay
  one `table_for` line each.
- **One validation path.** Field markers and a `clean()` method are
  checked by the aggregate itself when it is made or changed (the API
  derives its schema from the same markers), and the user model is an
  aggregate like any other, so `phone: Annotated[str, Pattern(...)]` is the
  whole of a validated user field.
- **One request flow, drawn in the docs**: route, controller, unit of
  work, aggregate, events after commit, outbox. Nothing else on the path
  that a reader cannot see.
- **Migration**: each step lands behind the current API, the guide's shop
  is rewritten on it and must shrink, and the old spelling is removed in
  the release after.

## Then

1. **The tutorial on the four-file layout** (done). It builds the bank on
   `domain.py`, `controllers.py`, `models.py`, and `routes.py`; a handler
   is a `@handles` function, and `test_walkthroughs.py` plays it as
   written.
2. **Field rules that hold everywhere** (done, C3). An aggregate checks
   its markers when it is made and changed, so a script or a task meets
   the same limits as the API, with the error as a 422.
3. **Custom user fields end to end** (done). Signup takes the user
   model's `REQUIRED_FIELDS` under `fields`, and a field's markers hold
   at signup, in the admin API, and in scripts, so a phone number is one
   line.
4. **One page per extension point** (done), each with a working, tested
   example walked by `test_recipes.py`: an authentication class
   ([API keys](recipes/api-keys.md)), a permission rule
   ([recipe](recipes/permission-rules.md)), an SMS provider
   ([recipe](recipes/sms-backend.md)), a cache store
   ([recipe](recipes/cache-store.md)), and a broker
   ([recipe](recipes/broker.md)); `CACHES` and `BROKERS` entries take a
   `BACKEND` of the project's own.
5. **Phone verification as an app** (done). `contrib.phones` sends a
   code through the outbox, keeps only its keyed digest, tries it
   `MAX_ATTEMPTS` times, limits sends per number and per user, and keeps
   the number verified ([phones.md](auth/phones.md)).
6. **A learning path in plain words** (done): the tutorial on the
   four-file layout, then [learning.md](learning.md): the order to read
   the guides in, a glossary, and the commands a day of work needs.

## Next: the best at what it is for

The aim is not to be the fastest of all Python frameworks (a bare ASGI
app answers in 5 calls) but the fastest, safest, and easiest framework
for services built around a domain, with every part replaceable. What
already holds is not repeated here: the free-threaded job (3.14t), the
security policy and its response times ([SECURITY.md](../SECURITY.md)),
the [extension points](reference/extension-points.md), the
[public API](versioning.md#what-is-public), the SBOM and its
attestation, and suggestions for a mistyped command or domain. Each step
below is one pushed commit with tests, as the core's were.

| # | Area | Step | What changes | Status |
| --- | --- | --- | --- | --- |
| L1 | Learning | Mistakes that name the fix | A mistyped settings key (`CACHES.default.URl`), filter (`?nmae=`), or query field is refused with the name it is near (`did you mean URL?`), as an unknown top-level setting, command, or domain already was. | Done |
| L2 | Learning | A first route before the words | A page that serves one route and one test before aggregates, units of work, and use cases appear, played as written by `test_walkthroughs.py`. | Planned |
| S1 | Security | Fuzzed input parsers | Property-based fuzz tests of what reads untrusted input (query filters and ordering, cursors, signed values, idempotency keys, conditional headers, multipart bodies): any input is answered or refused, never a 500; a scheduled run tries far more examples. | Planned |
| S2 | Security | OpenSSF Scorecard | The Scorecard workflow on `main` and weekly, its results in code scanning, and its badge. | Planned |
| E1 | Efficiency | Memory and startup budgets | The benchmark ladder records the memory a worker allocates to start and to answer its first request, and the time to start, with budgets CI holds as it holds calls. | Planned |
| P1 | Speed | The posting rung | A ledger posting costs about 12,000 calls, four times a command's: profiled, and each cut that keeps its guarantees made, with the budget lowered. | Planned |
| P2 | Speed | Routes resolved at startup | The largest remaining cost of a resource route is FastAPI resolving its parameters on each request (C4); a resource route's parameters are resolved by a plan made at startup, with the same validation and errors. | Planned |
| X1 | Customizing | Packages that plug in | An installed package offers backends under the `domainkit.backends` entry point, so `BACKEND = "sqlite-queue"` names one; nothing is loaded that settings do not name. | Planned |
| F1 | Follow-up | Generated text reads right | "An account", not "a account"; a new event goes with the other events. | Done |
| F2 | Follow-up | The newest-dependencies job | An unclosed SQLite connection seen once on Python 3.15 with the newest dependencies: reproduced and fixed, or shown to be upstream's. | Planned |

What code alone cannot give, and needs others:

- **An audit from outside**: a firm's review of the threat model, auth,
  signing, and the money paths ([the last internal audit](audit/0.9.0.md)
  is the starting scope).
- **A published comparison**: the same service on domainkit, Django,
  Litestar, and FastAPI, on the same machine, with the scripts to rerun it.
- **Newcomers timed**: how long people new to domainkit take to deploy a
  first route, before and after L1 and L2.
- **The whole suite on 3.14t**: it waits for the interpreter's fix for a
  crash in mimalloc ([runtime.md](runtime.md)).

Mutation testing stays off by decision; coverage stays at 98% or more.
