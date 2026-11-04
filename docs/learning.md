# Learning domainkit

This page helps you learn domainkit after [the tutorial](tutorial.md).
It gives an order in which to read the guides, explains each word the
pages use, and lists the commands you need on a normal working day.

On this page you find:

- [the path](#the-path): which guides to read, and in which order;
- [the words](#words): a glossary of every domainkit word, with an
  example;
- [a day of work](#a-day-of-work): the commands you run most.

## The path

Read the guides in this order. Each one builds on the ones before it.

1. **[The tutorial](tutorial.md)**: six parts of about 15 minutes each.
   You build a project and one route, then a domain, rules, permissions,
   an event that the worker handles, tests, and the checks production
   runs.
2. **[Resources](resources.md)** and **[validation](validation.md)**:
   more aggregates, the routes made from them, and the limits a field or
   an aggregate keeps.
3. **[The request flow](request-flow.md)**: what happens between a
   request and its answer, and what each step costs.
4. **[Queries](queries.md)** and **[databases](databases.md)**: filters,
   pages, locks, migrations, and the tables made from your classes.
5. **[Money](money.md)**: exact amounts, currencies, and rounding.
6. **[Users and permissions](auth.md)**: logins, tokens, and who may call
   which route.
7. **[Messaging](contrib/messaging.md)**: the outbox, background tasks,
   and the worker.
8. **[Testing](testing.md)**: the pytest plugin and its fixtures.
9. **[Deploying](deploying.md)** and **[operations](operations.md)**:
   from a project whose checks pass to one that serves in production.

If you know Django or FastAPI, [coming from Django](from-django.md) and
[coming from FastAPI](from-fastapi.md) map each of their names to
domainkit's. If you have one task to do, the
[how-to recipes](recipes.md) each solve one, and the tests run them as
written.

## Words

Each domainkit word has one meaning on every page. This glossary gives
that meaning, in alphabetical order. A word in *italics* has its own
entry.

### Access rule

What a route asks of the caller before it runs: a login, a
*permission*, a *step-up*, or a rule of your own. Every route has one, so
no route is public by accident. Also called a **rule**.
Example: `dependencies=[Public]` lets anyone call a route, and
`Depends(Require(IsAuthenticated()))` lets any logged-in user call it.
See [protecting routes](auth/routes.md#protecting-routes).

### Aggregate

A group of objects that the business changes as one whole, with an id.
Its methods hold the business rules, and only its methods change it, so
every way in meets the same rules. An aggregate is a plain Python class,
not a table: `models.py` maps it to one. The class you write is the
**aggregate root**; its *entities* are stored with it.
Example: `class Account(AggregateRoot[AccountId]): ...`.
See [resources](resources.md).

### Approval

A *command* that runs only after people other than the one who asked
for it approve it. This control is also called **maker-checker**.
Example: a payment above a limit waits for a second person.
See [approvals](contrib/approvals.md).

### Audit trail

The record of who did what, when, and from where. Each committed
*domain event* becomes one row of it, written in the same transaction as
the change. The worker **seals** the rows into a hash chain, so a changed
row can be detected.
Example: `python manage.py audit log --actor alice`.
See [audit](contrib/audit.md).

### Broker

A service that carries the *outbox*'s messages to the *workers*:
RabbitMQ, Redis Streams, NATS JetStream, Kafka, or Amazon SQS. Without a
broker, the workers read the outbox table themselves.
Example: `BROKERS = {"default": {"BACKEND": ...}}`.
See [messaging](contrib/messaging.md).

### Check

A test of the settings or the code, which `python manage.py check` and
startup run. Each check has an id: a namespace, then `E` for an error,
`W` for a warning, or `I` for a note, then a number. Staging and
production refuse to start with an error.
Example: `deploy.W004` warns that the database pool has no size.
See [deployment checks](reference/checks.md).

### Command

The request to run a *use case*, made from the use case's parameters.
A command is what a route's body holds, and what a *task* carries.
Commands are named in the imperative.
Example: the use case `deposit_account(uow, account_id, amount)` has the
command `DepositAccount(account_id=..., amount=...)`.

### Content type

A record of one kind of stored object, such as `accounts.account`.
*Permissions* are attached to content types. The app
`domainkit.contrib.contenttypes` keeps them.
See [installing auth](auth/install.md).

### Controller

The file `controllers.py` of a domain. It holds the domain's *use cases*
and its *handlers*.

### Criteria

A condition on aggregates, which a *repository* turns into a query. You
build criteria with `F`, and combine them with `&`, `|`, and `~`.
Example: `await uow.accounts.filter(F("archived") == False)`.
See [queries](queries.md).

### Crypto-shredding

Erasing a person's data by destroying the key that encrypted it. The
encrypted values stay, in the database and in old backups, but no one
can read them any more.
Example: `python manage.py privacy erase --subject <id>`.
See [keys](contrib/keys.md#erasing-a-person-crypto-shredding).

### Cursor

An opaque string that marks where one page of a list ended. You send it
back to get the next page. These pages are **keyset pages**: they stay
correct when rows are added while a client reads them.
Example: a list answers `{"items": [...], "next_cursor": "..."}`, and
`?cursor=...` asks for the next page.
See [pages](queries.md#pages).

### Dead letter

A message of the *outbox* that failed on each of its tries. The worker
stops running it and keeps it, so you can read it and run it again.
Example: `python manage.py messages list dead`, then
`python manage.py messages retry all`.

### Deadline

The time by which a request must start its answer: `REQUEST_TIMEOUT`
seconds after it arrives, 30 by default. Database calls and outgoing
calls stop at the deadline, and the client gets `503`.
See [request deadlines](runtime.md#request-deadlines).

### Deny by default

The rule that a route with no *access rule* is refused, not open.
`check` lists such routes, and staging and production refuse to start.
See [deny by default](auth/routes.md#deny-by-default).

### Domain

One part of the business, with its own words and rules, such as accounts
or billing. It is also the Python package that holds that part: by
default, four files (`domain.py`, `controllers.py`, `models.py`,
`routes.py`). Inside one domain, each word has one meaning. Domain-driven
design calls this a **bounded context**.
Example: `python manage.py startdomain billing --aggregate Invoice`.
See [generators](resources.md#generators).

### Domain event

A fact that an *aggregate* records when it changes, named in the past
tense. It is published after the commit, so it describes only what
really happened.
Example: `self.record(AccountDeposited(account_id=self.id, amount=amount))`.
See [the tutorial, part 4](tutorial/4-events.md).

### Durable

Said of a *handler* or a *task* that is stored in the *outbox* with the
commit, and run by the *worker*. A durable handler runs even if the
process stops right after the commit.
Example: `@handles(AccountDeposited, durable=True)`.

### Entity

An object with an id that lives inside an *aggregate*, and is stored and
deleted with it. Two entities are equal when their ids are equal.
Example: the lines of an order.
See [child entities](databases.md#child-entities).

### Event bus

The object that delivers each *domain event* to the *handlers*
subscribed to its class. A *unit of work* hands its events to the bus
after the commit.
Example: `event_bus.subscribe(AccountDeposited, notify)`.

### Evidence

A record that must never change, such as a signed consent. It is
written to a *ledger*, which proves that it did not change.
See [evidence](contrib/evidence.md).

### Factory

In tests, a class that makes objects with default values, so that each
test sets only the fields it cares about.
Example: `class AccountFactory(Factory[Account]): ...`.
See [factories](testing.md#factories).

### Feature flag

A switch, kept in the database, that turns a feature on for everyone,
for no one, or for some users, without a deployment.
See [flags](contrib/flags.md).

### Fixture

A pytest fixture: a value that pytest gives to a test that names it as a
parameter. domainkit's plugin gives fixtures such as `client`,
`admin_client`, and `outbox`. (`loaddata` reads *data* fixtures: files of
rows. That is a different thing.)
Example: `def test_rates(client: TestClient) -> None: ...`.
See [testing](testing.md).

### Generator

A command that writes code for you: `startdomain`, `startaggregate`,
`addcommand`, `addquery`, and `addevent`.
Example: `python manage.py addcommand accounts Account DepositAccount --field amount:money`.
See [generators](resources.md#generators).

### Handler

An async function that reacts to a *domain event*, marked with
`@handles`. A handler that is not *durable* runs in the same process,
after the commit.
Example: `@handles(AccountDeposited) async def notify(event): ...`.
See [event handlers](runtime.md#event-handlers).

### Hold

An amount of money set aside on an account of the double-entry books.
The amount is no longer available, but it has not moved yet. A hold is
later captured or released.
See [accounting](contrib/accounting.md).

### Idempotency key

A header, `Idempotency-Key`, that a client sends with a `POST` or
`PATCH`. A retry with the same key gets the first answer again, and the
work does not run twice.
See [idempotency](contrib/idempotency.md).

### Inbox

A table of the messages whose database work has already committed. A
task or handler marked `@inbox` commits its work once for each message,
even when the message runs twice.
See [the inbox](contrib/messaging.md#once-in-the-database-the-inbox).

### Layer

One of the four parts of a domain: the domain itself (`domain.py`), the
application (`controllers.py`), the storage (`models.py`), and the API
(`routes.py`). `check` keeps each layer to the imports it may use.
Example: `domain.py` may not import SQLAlchemy.
See [layer rules](reference/settings.md#layer-rules).

### Ledger

A database that keeps records which no one can change or remove later,
not even its owner: immudb. *Evidence* and the *audit trail*'s anchors
go there. (The double-entry books of *accounting* are a different
thing.)
See [ledgers](databases.md#ledgers-immudb).

### Load shedding

Refusing extra requests quickly when a process already works on as many
as it can (`MAX_CONCURRENT_REQUESTS`). A few requests may wait for a
short time; the others get `503` with the code `overloaded` at once. So
the requests already accepted still finish in time.
See [load shedding](runtime.md#load-shedding).

### Marker

A limit that you put on a field inside `Annotated[...]`. The object,
the request, and the table all check it.
Example: `name: Annotated[str, MaxLength(100)]`.
See [validation](validation.md).

### Middleware

Code that runs around every request, before and after the route. The
setting `MIDDLEWARE` lists it, in order.
See [middleware](reference/http.md#middleware).

### Migration

A change to the database tables, written from your classes by
`makemigrations` and applied by `migrate`. A **data migration** changes
rows instead of tables; you write it into an empty migration.
Example: `python manage.py makemigrations -m "account balance"`.
See [databases](databases.md).

### Money

An exact amount and its currency, never a float. Amounts of two
currencies never add up.
Example: `Money.of("10.50", "USD")`.
See [money](money.md).

### Ordering key

A value that makes the worker run some messages one at a time, in the
order of their commits: all the messages with the same key, such as the
same account. You mark the task or durable handler with `@ordered`.
See [ordering keys](contrib/messaging.md#one-at-a-time-per-key-ordering-keys).

### Outbox

A table in the same database as your data. A *durable handler* or a
*task* is written to it in the same transaction as the change. The
*worker* runs it after the commit. If the change rolls back, the message
is never stored.
Example: `python manage.py messages list pending`.
See [messaging](contrib/messaging.md).

### Passkey

A key on a user's device that logs the user in with no password and no
second factor (WebAuthn). A passkey can also *step up* a user.
See [passkeys and bound devices](auth/devices.md).

### Permission

A right to do one action on one kind of data. Its name is the domain's
label, a dot, the action (`view`, `add`, `change`, or `delete`), and the
model's name. You grant permissions to users and to groups. A grant on
one object only is an **object permission**.
Example: `accounts.view_account`.
See [users and groups](auth/users.md).

### Personal data

A field that holds data about a person, marked with `Personal`. The
exports, erasures, and audit trail of a person's data find their fields
by this marker.
Example: `email: Annotated[str, Personal(of="user_id")]`.
See [privacy](privacy.md).

### Port

What the domain needs from outside, declared as a class in `domain.py`:
its *repository* and its *unit of work*. `models.py` fills the ports with
a database. So the domain's code never imports the database.
Example: `class AccountsUnitOfWork(UnitOfWork, ABC): accounts: AccountRepository`.

### Principal

Who the credentials of a request name: a user, with what they may do.
Each process keeps principals for a few seconds, so that a request does
not read them from the database each time.
See [the principal cache](auth/authentication.md#the-principal-cache).

### Problem code

The machine-readable `code` of an error answer, such as
`invalid_amount`. A code keeps its meaning across releases, so clients
can act on it.
Example: `{"detail": "A deposit must be positive.", "code": "invalid_amount"}`.
See [problem codes](reference/problems.md).

### Query

A request for data that changes nothing, as opposed to a *command*.
`addquery` adds one to a domain.
See [resources](resources.md#queries).

### Reference

The id of another *aggregate*, kept in a field. Aggregates refer to each
other by id only, and you load each one on its own.
Example: `customer_id: Reference[CustomerId]`.
See [databases](databases.md).

### Replica

A copy of a database that takes reads off the main one. A *unit of
work* made with `readonly=True` reads from a replica.
See [reading from a replica](recipes/read-replica.md).

### Repository

Where the *aggregates* of one kind are kept and found: `get`, `add`,
`filter`, `paginate`, and more. You declare it as a class with no code;
domainkit makes the SQL one and an in-memory one for tests.
Example: `account = await uow.accounts.get(account_id)`.
See [queries](queries.md).

### Resource

The HTTP side of an *aggregate*: a `ResourceRouter` that lists and reads
the aggregates, and runs its *use cases* as routes.
Example: `accounts.use_case(controllers.deposit_account, path="/deposit")`.
See [resources](resources.md).

### Row lock

A lock on the rows a query reads, held until the transaction ends. No
other transaction can change those rows in the meantime
(`SELECT ... FOR UPDATE`).
Example: `account = await uow.accounts.get_for_update(account_id)`.
See [row locks](queries.md#row-locks).

### Rule

See *access rule*. In the domain, a business rule is a check in an
*aggregate*'s method that refuses a change, with a
`BusinessRuleViolation`.

### Savepoint

A point inside a transaction that you can go back to. If the block
inside a savepoint raises, only that block is undone; what came before
stays.
Example: `async with uow.savepoint(): ...`.
See [savepoints](queries.md#savepoints).

### Scope

A *permission* that limits what a token may do. A token with scopes may
do only what both its user and its scopes allow.
Example: `python manage.py createtoken alice --scope shop.view_product`.
See [personal tokens and scopes](auth/authentication.md#personal-tokens-and-scopes).

### Second factor

A second proof of who you are, after the password: a code from an
authenticator app (TOTP), a recovery code, or a passkey.
See [two-factor logins](auth/logging-in.md#two-factor-logins).

### Settings module

A Python module of UPPERCASE names that configures a project. An
environment variable selects it: `<PREFIX>SETTINGS_MODULE`, where the
prefix comes from the project's name.
Example: `BANK_API_SETTINGS_MODULE=config.settings.production`.
See [settings](reference/settings.md).

### Shard

One of several databases that each hold a part of the same tables, split
by a key.
See [sharding](databases.md#sharding).

### Specification

A named business rule about aggregates, which works both in Python and
in queries. You write the rule once, as *criteria*.
Example: `Overdue(today).is_satisfied_by(invoice)` checks one invoice,
and `await uow.invoices.filter(Overdue(today))` finds them all.
See [specifications](queries.md#specifications).

### Step-up

Proving again who you are, with a second factor or a passkey, before a
sensitive act such as adding a payee.
See [stepping up](auth/step-up.md).

### Superuser

A user who holds every *permission*. A **staff** user (`is_staff`) may
use the admin API, within their permissions.
Example: `python manage.py createsuperuser`.

### Task

An async function that runs in the background, in the *worker*. You
queue it with `uow.defer(...)` inside a *unit of work*, so it is stored
with the commit. A **periodic** task runs on a schedule.
Example: `@task async def send_statement(account_id: UUID) -> None: ...`.
See [messaging](contrib/messaging.md).

### Tenant

One customer of a service that serves many customers. Each tenant's
rows, caches, and limits are its own.
See [multi-tenancy](tenancy.md).

### Unit of work

One transaction over a domain's *repositories*. You open it, change
aggregates, and commit everything together, or roll everything back. A
*use case* runs inside one. On commit it also publishes the *domain
events* and stores the *durable* work in the *outbox*.
Example: `async with uow: ...; await uow.commit()`.
See [transactions](databases.md#transactions).

### Use case

One thing the domain does, written as one async function marked
`@use_case` in `controllers.py`. It loads aggregates, calls their
methods, and returns. The API, a script, and a test all call the same
function. Its parameters, after the unit of work, make its *command*.
Example: `await deposit_account(uow, account_id=..., amount=...)`.
See [resources](resources.md#use-cases).

### Value object

A value with no id, equal to another value when what it holds is equal.
It does not change: you make a new one instead.
Example: `Money.of("1.00", "USD") == Money.of("1.00", "USD")` is `True`.

### Version

A number on each *aggregate* that grows by one at each change. Each read
answers it as an `ETag`, and a change can send it back in `If-Match`. A
change made from an old version is refused.
See [versions and conditional requests](resources.md#versions-and-conditional-requests).

### Webhook

An HTTP request that your service sends to another service's URL when a
domain event happens. It is signed, so the receiver can check that it
came from you.
See [webhooks](contrib/webhooks.md).

### Worker

The process that runs the *outbox*'s messages: *durable handlers* and
*tasks*. In production it runs beside the web processes.
Example: `python manage.py runworker`.
See [the worker](operations.md#the-worker).

## A day of work

Run each command from the project's directory. Each one answers
`--help` with its options. [Commands](reference/commands.md) lists all
of them.

| To | Run |
| --- | --- |
| Serve the project, and restart it when the code changes | `python manage.py runserver` |
| Run background tasks and durable handlers | `python manage.py runworker` |
| Run the tests | `pytest` |
| Check the settings, the routes, and the layers | `python manage.py check` |
| Start a domain | `python manage.py startdomain billing --aggregate Invoice` |
| Add an aggregate, a command, a query, or an event | `startaggregate`, `addcommand`, `addquery`, `addevent` |
| Write a migration after you change fields | `python manage.py makemigrations -m "invoice due date"` |
| Apply the migrations | `python manage.py migrate` |
| See which migrations are applied | `python manage.py showmigrations` |
| Write an empty migration, to fill in by hand | `python manage.py makemigrations --empty -m "split names"` |
| Make a user who may do everything | `python manage.py createsuperuser` |
| Change a user's password | `python manage.py changepassword alice` |
| Make a token for a user's scripts | `python manage.py createtoken alice` |
| Open a Python shell, or the database's shell | `python manage.py shell`, `python manage.py dbshell` |
| See the outbox | `python manage.py messages list pending`, `messages list dead` |
| Run failed messages again | `python manage.py messages retry all`, or one message's id |
| Load or save rows | `python manage.py loaddata`, `dumpdata` |
| Show the settings that differ from the defaults | `python manage.py diffsettings` |
| Print the API's OpenAPI document | `python manage.py openapi` |
| Find messages to translate, and compile the translations | `python manage.py makemessages`, `compilemessages` |
