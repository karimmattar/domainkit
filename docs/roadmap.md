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

## Done in 1.7 and 1.8

| Release | What it gives |
| --- | --- |
| 1.7 | The fixes of the outside review's first run (A1 to A7), seven of them breaking ([upgrading.md](upgrading.md#16-to-17)); the documentation rewritten, a guide page for each part, in plain English; about 98 calls fewer on every authenticated route. |
| 1.7.1 | `domainkit init NAME .` writes into an existing directory, such as a fresh clone. |
| 1.8 | Users identified by the fields a project chooses (`AUTH.IDENTIFIERS`), such as an email address or a mobile number; the username optional; codes of 6 digits instead of links for email verification, password resets, and signups, by mail or text message; new projects log in by email or number ([ADR 0126](adr/0126-identify-users-by-the-fields-a-project-chooses-and-verify-them-by-code.md), [upgrading.md](upgrading.md#17-to-18)). |

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
| L2 | Learning | A first route before the words | [Part 0](tutorial/0-first-route.md) of the tutorial serves one public route and its test before aggregates, units of work, and use cases appear; `test_walkthroughs.py` plays it as written, and fails if it uses those words. | Done |
| S1 | Security | Fuzzed input parsers | `test_fuzz.py` sends anything (any text, any bytes, valid input broken at random) to what reads untrusted input: query strings through a filter set, `If-Match` and `If-None-Match`, `traceparent` and `tracestate`, signed values, WebAuthn authenticator data, CBOR, client data, and base64url, and `Accept` and `Accept-Language`; each answers or raises its one documented error, never a 500. Cursors were fuzzed already (`test_properties.py`); the idempotency key is only hashed. The weekly run tries fifty times the examples (`--hypothesis-profile fuzz`); 5,000 a parser found nothing to fix. | Done |
| S2 | Security | OpenSSF Scorecard | The Scorecard workflow on `main` and weekly, its results in code scanning, and its badge. | Done |
| E1 | Efficiency | Memory and startup on the ladder | Each rung records its process's peak memory once it has served (26 MiB for bare ASGI, 46 for FastAPI, 90 to 110 for domainkit), gated with a tolerance of a tenth as modules and calls are, and the time it took to start, reported (time is never gated, ADR 0064; modules, gated, are most of it). | Done |
| P1 | Speed | The posting rung | A posting cost 11,973 calls; now 8,364 (−30%), with the same 14 statements and guarantees. It books the entry it checked instead of drafting it twice, and the statements limits and the ledger run on every posting (the limits of its subjects, their usage, each subject's lock, the count, the closed books, the entry under its reference, the covering period) are made once, so SQLAlchemy no longer builds them and walks them for a cache key on each request. What is left is the repository's generic locking read and the ORM's flush, which P2's work on the request path reaches. | Done |
| P2 | Speed | A statement per query shape | Measured again, FastAPI resolving a route's parameters is now about a tenth of a resource route; more goes to SQLAlchemy building, and walking for a cache key, a new statement for every query. A repository's `filter` now keeps the statement of each shape of query (the same fields, lookups, ordering, limit, and lock, with values of the same types) and binds the values as parameters, typed as SQLAlchemy types the literals; exact, `in`, and the comparisons on the table's own columns take part, and money, encrypted, related, and text lookups compile as before. A resource read costs 2,088 calls instead of 2,359, a command 2,829 instead of 3,083, a page 2,865 instead of 3,025, and a posting 8,148. | Done |
| X1 | Customizing | Packages that plug in | An installed package offers backends under the `domainkit.backends` entry points, so `BACKEND = "sqlite-queue"` names one: caches, brokers, storage, search, upload scanning, mail, text messages, and push ([extension points](reference/extension-points.md#backends-from-an-installed-package)). Nothing is loaded that settings do not name; an unknown name is refused with the nearest one offered, and a name two packages offer differently is refused. | Done |
| W1 | Speed | Writes | Measured again: FastAPI resolving a route's dependencies is the largest block of a write (about 600 calls of a posting, 560 of a resource command), and each dependency costs about a hundred calls even when it does nothing. A guarded route no longer resolves `bearer`, which only puts the token in OpenAPI: every authenticated route costs about 98 calls less (a write 2,054, a resource command 2,726, a posting 8,049). What is left on a write is spread thin: FastAPI's own work for each remaining dependency (the access rule, the unit of work), SQLAlchemy's execution of each statement, and building `Money` from stored amounts (about 300 calls of a posting). Each further cut is a few percent, and the last touches money's checks; none is taken without a measured need. | Done |
| F1 | Follow-up | Generated text reads right | "An account", not "a account"; a new event goes with the other events. | Done |
| F2 | Follow-up | The newest-dependencies job | An unclosed SQLite connection (aiosqlite) was seen once on Python 3.15 with the newest dependencies. It has not come back in that job's later runs, and it cannot be reproduced locally (the local 3.15 beta cannot import Hypothesis's compiled part). Not seen again: the job passed in every run since, the weekly runs of 26 October and 2 November among them, and on the last pull requests before this one (#106, #107). It keeps running on every pull request and weekly, so a return shows there. | Closed |
| P3 | Speed | Reads under load | The first, indicative run of the comparison scripts (`benchmarks/compare`) measured a read at a median of 92 ms where FastAPI answered in 15 ms, under the same load. Measured again: the medians compared two queues, not two costs. With 64 connections one process is saturated; FastAPI lets most requests through and makes some wait long (a median of 12 ms, a 90th percentile of 165 ms), at a mean of 52 ms against this framework's 79 ms. With one connection, a read takes 1.71 ms against 1.14 ms: 1.5 times FastAPI's SQLAlchemy Core app, a gap spread across the ORM's unit of work (about a quarter of a read) and the default middleware (1 to 4% a layer). The comparison now reports the mean ([how to read it](https://github.com/karimmattar/domainkit/blob/main/benchmarks/compare/README.md#reading-the-numbers)). Since: a busy asyncpg pool no longer pings each connection it hands out ([ADR 0127](adr/0127-ping-a-pooled-connection-only-after-it-rested.md)), saving a round trip per connection a request takes, and `Reads.STATEMENTS` reads skip parsing the URL. The ladder's database rungs send no pings and make 2 to 4% fewer calls. What is left is the ORM's own work per read. | Measured |
| S3 | Security | The review's leftovers | Pruning the audit trail, and the startup checks, visit each tenant's schema, as sealing and verifying do since 1.7 (A6-4); the project template pins its base image by digest. | Done |
| R1 | Removal | Links, in 2.0 | Links for email verification, password resets, and signups, deprecated in 1.8, are removed; codes are the only way ([upgrading.md](upgrading.md#18-to-20)). | Done for 2.0 |
| C1 | Identity | The phone as identity, in 2.1 | Web sign-ins, step-ups and payments approved from the user's mobile app, by push or QR code, with device enrollment and a strict mode where every login is signed or approved by a bound device; OpenID Connect CIBA for other clients ([Sign-in approvals](auth/ciba.md), ADRs 0128 and 0129). | Done for 2.1 |
| C2 | Identity | Logins managed from the phone, in 2.2 | Each login records how it signed in, its browser and address; the phone lists the web sessions and ends one, all but itself, or all. In the phone modes, only the phone manages logins, devices and second factors, and a project chooses what a credential change does to the phone's login ([Sessions](auth/logging-in.md#sessions), ADRs 0130 to 0132). | Done for 2.2 |

What code alone cannot give, and needs others:

- **An audit from outside**: a firm's review of the threat model, auth,
  signing, and the money paths. [The scope to give them](audit/scope.md)
  is ready, with the areas in order and their size. The review's first
  run (A1 to A7) is fixed in 1.7; a firm's own review waits.
- **A published comparison**: the same service on domainkit, Django,
  Litestar, and FastAPI, on the same machine. The scripts are ready
  (`benchmarks/compare`, with its README); each app answers right, and
  the numbers wait for a quiet machine.
- **Newcomers timed**: how long people new to domainkit take to deploy a
  first route, before and after L1 and L2. [The protocol](newcomer-study.md)
  is ready; it waits for five people.
- **The whole suite on 3.14t**: it waits for the interpreter's fix for a
  crash in CPython's weak references, seen again in five runs in November
  2026 ([runtime.md](runtime.md#free-threaded-python)).

Mutation testing stays off by decision; coverage stays at 98% or more.
