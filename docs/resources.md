# Resources

A [resource](learning.md#words) is the HTTP side of an aggregate. One
`ResourceRouter` lists and reads one kind of aggregate, and runs its use
cases as routes. Each request is one use case, and the domain decides
what changes. You declare the resource once; it gives you pages, filters,
`ETag`s, conditional writes, access rules, and an OpenAPI document that
says all of it.

On this page you learn how to:

- write the domain a resource serves: aggregates, value objects, events,
  commands, and use cases;
- turn use cases and commands into routes, with bodies made from their
  parameters;
- answer queries, run batches, and export listings as CSV;
- derive request and response schemas from your classes (`schema_for`);
- make writes conditional with `ETag` and `If-Match`;
- say who may call each route, and who sees each field;
- nest resources, and generate whole domains with the generators;
- serve a generic admin.

If you know Django REST framework: a `ResourceRouter` does the work of a
viewset, a router, and a serializer, for task-based commands.

## When to use it, and when not to

- **Use it when** clients list, read, and change one kind of aggregate.
  One declaration gives pages, filters, `ETag`s, conditional writes,
  access rules, and the OpenAPI document.
- **Do not use it when** a route serves no aggregate, such as a health
  page or the rates of [tutorial part 0](tutorial/0-first-route.md):
  write a FastAPI route with an access rule. For one more route on a
  resource's aggregate, use [`fetch`](#routes-of-your-own-on-a-resource),
  which keeps the resource's reach and rules.
- [Choosing a feature](choosing.md#serving-a-route) compares the ways to
  serve a route.

## A first example

A product, two use cases, and the resource that serves them:

```python
from abc import ABC
from typing import Annotated, NewType, Self
from uuid import UUID, uuid4

from fastapi import APIRouter

from domainkit.ddd import (
    AggregateRoot,
    DomainEvent,
    MaxLength,
    NotFound,
    Range,
    Repository,
    UnitOfWork,
    in_memory,
    use_case,
)
from domainkit.resources import ModelActions, ResourceRouter

ProductId = NewType("ProductId", UUID)
ProductName = Annotated[str, MaxLength(100)]


class ProductNotFound(NotFound):
    """No product has that id."""


class ProductRestocked(DomainEvent):
    product_id: ProductId
    quantity: int


class Product(AggregateRoot[ProductId]):
    name: ProductName
    stock: Annotated[int, Range(min=0)] = 0

    @classmethod
    def create(cls, name: str) -> Self:
        return cls(id=ProductId(uuid4()), name=name)

    def restock(self, quantity: int) -> None:
        self.stock += quantity
        self.record(ProductRestocked(product_id=self.id, quantity=quantity))


class ProductRepository(Repository[Product, ProductId]):
    def not_found(self, id_: ProductId) -> ProductNotFound:
        return ProductNotFound(f"Product {id_} does not exist.")


class CatalogUnitOfWork(UnitOfWork, ABC):
    products: ProductRepository


@use_case
async def create_product(uow: CatalogUnitOfWork, name: ProductName) -> Product:
    """Create a product."""
    product = Product.create(name)
    await uow.products.add(product)
    return product


@use_case
async def restock(
    uow: CatalogUnitOfWork,
    product_id: UUID,
    quantity: Annotated[int, Range(min=1, max=1000)],
) -> Product:
    """Add items to a product's stock."""
    product = await uow.products.get(ProductId(product_id))
    product.restock(quantity)
    return product


store = in_memory(CatalogUnitOfWork)


async def get_unit_of_work() -> CatalogUnitOfWork:
    return store  # in production, SqlAlchemyCatalogUnitOfWork()


router = APIRouter()
products = ResourceRouter(
    "/products",
    model=Product,
    unit_of_work=get_unit_of_work,
    repository=lambda uow: uow.products,
    rule=ModelActions(Product),  # view, add, change, delete
)
products.use_case(create_product)  # POST /products
products.use_case(restock, path="/restock")  # POST /products/{product_id}/restock
router.include_router(products, prefix="/api/v1/catalog")
```

Mounted in a project where the `catalog` domain is installed, the
resource gives a caller who holds the permissions these answers:

```text
POST /api/v1/catalog/products  {"name": "Lamp"}
201 Created
Location: /api/v1/catalog/products/fc234ff8-ae83-4551-9196-b407a0a61d1f
ETag: W/"0-95d51920a9ce3578"
{"id": "fc234ff8-...", "version": 0, "name": "Lamp", "stock": 0}

POST /api/v1/catalog/products/fc234ff8-.../restock  {"quantity": 3}
200 OK
ETag: W/"0-fe42f95d70d5f3d8"
{"id": "fc234ff8-...", "version": 0, "name": "Lamp", "stock": 3}

GET /api/v1/catalog/products
200 OK
{"items": [{"id": "fc234ff8-...", "version": 0, "name": "Lamp", "stock": 3}], "next_cursor": null}

GET /api/v1/catalog/products/fc234ff8-...  If-None-Match: W/"0-fe42f95d70d5f3d8"
304 Not Modified
```

The version stays 0 because the example keeps its products in memory
(`in_memory`). A database moves the version with each change
([versions](databases.md#versions)).

A `{"quantity": 0}` body is refused before the function runs:

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The request is invalid.",
  "code": "validation_error",
  "errors": [
    {"loc": ["body", "quantity"], "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal"}
  ]
}
```

| Route | Answer |
| --- | --- |
| `GET /products` | A page: `{"items": [...], "next_cursor": ...}`. With a `FilterSet`, filtered, ordered, and searched; paged by `Pagination` ([queries.md](queries.md)). |
| `GET /products/{product_id}` | The product, with an `ETag`. `If-None-Match` answers `304`. |
| `POST /products` | `create_product`, run in its unit of work; `201` with `Location` and `ETag`. |
| `POST /products/{product_id}/restock` | `restock` on one product; the id comes from the path. `If-Match` makes it conditional. |

## The domain behind a resource

A resource serves a domain written in plain Python, with
`domainkit.ddd`. That package depends on nothing but the standard library
and a few of domainkit's own modules. So a domain layer that imports only
`domainkit.ddd` stays free of frameworks and I/O.

| Building block | Base class | What it is |
| --- | --- | --- |
| [Aggregate](learning.md#words) | `AggregateRoot[IdT]` | A thing the business changes as a whole, with an id and a `version`. It records events. |
| Entity | `Entity[IdT]` | An object with an id inside an aggregate, stored with it (an order's lines). Equal when the ids match. |
| Value object | `ValueObject` | An immutable value with no id, equal when its fields are (an amount, a place). |
| Domain event | `DomainEvent` | A fact that happened, named in the past tense. |
| Command | `Command` | A request to change something, named in the imperative. |
| Query | `Query` | A request for data that changes nothing. |

Every one of them becomes a dataclass by itself. Fields are keyword-only,
so you may add fields with or without defaults in any order. A field's
markers are checked when it is set ([validation](validation.md)).

```python
from decimal import Decimal
from typing import Annotated

from domainkit.ddd import BusinessRuleViolation, Digits, ValueObject


class NegativePrice(BusinessRuleViolation):
    """A price is never below zero."""


class Price(ValueObject):
    amount: Annotated[Decimal, Digits(12, 2)]

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise NegativePrice("A price is never below zero.")
```

A value object checks itself in `__post_init__` and raises a
`DomainError` subclass, so an invalid value can never exist.

**Field markers.** A marker is metadata in `Annotated[...]` that says
more about a field. The same markers reach the request schemas, the
tables, and the privacy tools:

```python
from typing import Annotated

from domainkit.ddd import Email, Encrypted, Indexed, MaxLength, Personal, Range, Unique


class Customer(AggregateRoot[CustomerId]):
    email: Annotated[Email, Unique(), Personal(of="id")]
    name: Annotated[str, MaxLength(100), Indexed()]
    age: Annotated[int, Range(min=18)]
    tax_number: Annotated[str | None, Encrypted(subject="id")] = None
```

| Markers | What they say | Explained on |
| --- | --- | --- |
| `MaxLength`, `MinLength`, `Pattern`, `Range`, `IPAddressFormat`; the types `Email`, `URL`, `Slug`, `IPAddress` | The values a field allows. | [Validation](validation.md) |
| `Digits` | The digits of an amount. | [Exact amounts](databases.md#exact-amounts) |
| `Unique`, `Indexed` | A unique constraint, an index. | [Unique values](databases.md#unique-values) |
| `DatabaseDefault`, `Generated` | A column the database fills. | [Columns the database fills](databases.md#columns-the-database-fills) |
| `Encrypted` | Stored sealed, never in clear. | [Encrypted fields](databases.md#encrypted-fields) |
| `Sensitive` | A secret no schema shows or takes, such as a password hash. | [Schemas](#schemas-from-the-domain) |
| `Field(repr=False)` (pydantic's) | A value no `repr` shows, such as a password a use case takes. | [Use cases](#use-cases) |
| `Personal(of=...)`, `Counted` | Personal data about someone; a collection the audit trail counts, not lists. `fields_about(holder, values, subject)` picks what a copy holds about one person. | [Privacy](privacy.md), [Audit](contrib/audit.md) |
| `Reference[XId]` | The id of another aggregate. | [Many-to-many](databases.md#many-to-many) |

**Events.** An aggregate records events with `self.record(event)`. The
unit of work publishes them after a successful commit, and never for work
that did not commit. `pending_events` shows what is recorded and not yet
published; `pull_events()` takes them. An event has an `event_id` and an
`occurred_at` of its own. Carry plain values in events (ids, text,
numbers), so they stay valid messages after the model changes.

**Who acted.** An event names who caused it in `actor`, an `Actor`, read
from the request, job, or command it was made in, as `occurred_at` is
when it was made
([ADR 0147](adr/0147-name-who-caused-each-event-and-tell-the-old-address.md)).

| Made in | `actor` |
| --- | --- |
| A request of a user | `Actor("<user id>")` |
| A request of staff impersonating a user | `Actor("<staff id>", impersonating="<user id>")` |
| A job a request queued (a durable handler, a task) | The actor of that request |
| A periodic task, or a job no request queued | `Actor("system")`: `is_system` is true |
| A command an operator ran | `Actor("operator:<name>")` |
| An anonymous request, or code outside all of these | `None` |

`actor.is_self(subject_id)` holds when the subject acted in their own
name: not staff acting on them, directly or as them, nor an operator, nor
the system. Pass `actor=` to an event to name someone else.
Webhook bodies, live events, and audit details leave `actor` out: the
audit row names the actor in its own column. A list of an event's own
fields, such as a test helper that leaves out `event_id` and
`occurred_at`, leaves out `actor` too.

```python
@handles(OrderCancelled, durable=True, name="shop.tell_the_customer")
async def tell_the_customer(event: OrderCancelled) -> None:
    by_staff = event.actor is None or not event.actor.is_self(event.customer_id)
    ...
```

**Handlers.** A function marked `@handles(ProductRestocked)` reacts to an
event. In a four-file domain, the handlers in `controllers.py` are
subscribed to `event_bus` when the domain is ready. `durable=True` runs
the handler from the outbox, in the worker, where it survives crashes and
is retried. An in-process handler that runs past its timeout is cut off
(`HandlerTimedOut`); `timeout=` gives it a limit of its own. An
`EventHandler` is any `async def handler(event) -> None`.
[Messaging](contrib/messaging.md) explains handlers, `EventBus`, and the
worker.

```python
from domainkit.ddd import handles


@handles(ProductRestocked, durable=True, name="catalog.tell_the_warehouse")
async def tell_the_warehouse(event: ProductRestocked) -> None: ...
```

**Errors.** A business rule refuses with a subclass of a `DomainError`.
The class name gives its `code`: `ProductNotFound` is
`product_not_found`. The HTTP layer picks the status from the base class:

| Base class | Status | `code` of the base |
| --- | --- | --- |
| `NotFound` | 404 | `not_found` |
| `Conflict` (and `ConcurrentUpdate`, `DuplicateValue`, `MultipleFound`, `LockNotAvailable`) | 409 | `conflict` |
| `PermissionDenied` | 403 | `permission_denied` |
| `PreconditionFailed` | 412 | `precondition_failed` |
| `BusinessRuleViolation` (and `InvalidField`, `CheckFailed`) | 422 | `business_rule_violation` |
| Any other `DomainError` | 400 | `domain_error` |

The message is shown to the client, so write it for people. Clients
switch on codes, so a code is public: set `code` on the class to keep it
when you rename the class. [Problems](reference/problems.md) lists every
code domainkit itself uses.

## Use cases

The shortest way to a route is one async function. `@use_case` makes a
`Command` from the parameters after the unit of work. It runs the
function inside that unit of work: opened, then committed, or rolled back
if it raises. `ResourceRouter.use_case` declares its route:

```python
from domainkit.ddd import Range, use_case


@use_case
async def restock(
    uow: CatalogUnitOfWork,
    product_id: UUID,
    quantity: Annotated[int, Range(min=1, max=1000)],
) -> Product:
    """Add items to a product's stock."""
    product = await uow.products.get(ProductId(product_id))
    product.restock(quantity)
    return product


products.use_case(restock, path="/restock")  # POST /products/{product_id}/restock
```

- **The body** is made from the parameters that the path and `provide`
  do not give, with their limits. `{"quantity": 0}` is `422` before the
  function runs. A body that sends `product_id` is refused, because the
  path names it. A use case whose fields the path gives whole takes no
  body.
- **The route** is named after the function (`restock`). It asks for the
  permission of its action: `change_product` on one product,
  `add_product` on the collection. It answers with the aggregate the
  function returns. A function declared `-> None` answers `204`.
- **Options.** `body=`, `returns=`, `method=`, `rule=`, `provide=`, and
  the other options of `command_route` apply.
- **In tests and scripts,** call it: `await restock(uow,
  product_id=..., quantity=3)`. `restock.command` is its `Command`
  (`Restock`). `restock.run(command, uow)` is the `service(command, uow)`
  that tasks and `command_route` take.
- **Secrets.** A parameter hinted `Field(repr=False)` (pydantic's, as a
  request model hides a password) is a command field with `repr=False`.
  The command's `repr`, and so a log line of it, leaves it out, and so
  does the body's. The body's validation errors leave the input out. A
  `Command` written by hand does the same with
  `dataclasses.field(repr=False)`.

```python
print(restock)
print(restock.command(product_id=product.id, quantity=2))
```

```text
<use case restock (Restock)>
Restock(product_id=UUID('3374d5e3-6b53-40cc-9be7-fdf68c20a508'), quantity=2)
```

```python
from pydantic import Field

from domainkit.ddd import MinLength, use_case


@use_case
async def change_password(
    uow: AccountsUnitOfWork,
    user_id: UUID,
    password: Annotated[str, MinLength(12), Field(repr=False)],
) -> None:
    """Set a user's password."""
    ...


print(change_password.command(user_id=user.id, password="correct horse"))
```

```text
ChangePassword(user_id=UUID('0b5f1c64-2c0e-4a8e-9a43-1d2f7e3b9c10'))
```

`@use_case(name="refill")` names the command (`Refill`) and the route
otherwise. The object is a `UseCase`, with `function`, `name`, `command`,
`fields`, and `returns_nothing`. `@use_case` refuses, with `TypeError`, a
function that is not async, takes no unit of work first, or has a
parameter without a type hint, positional-only, `*args`, or `**kwargs`.

## Commands

A command class and a service are the long form of a use case.
`command_route(Command, service=..., body=...)` builds the command from
the body's fields and the path's ids. It then awaits `service(command,
uow)`, which opens the unit of work, changes the aggregate, and commits:

```python
from domainkit.ddd import Command


class RenameProduct(Command):
    """Rename a product."""

    product_id: UUID
    name: ProductName


async def rename_product(command: RenameProduct, uow: CatalogUnitOfWork) -> Product:
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
        product.rename(command.name)
        await uow.commit()
    return product


RenameIn = schema_for(RenameProduct, write=True, include=["name"])
products.command_route(
    RenameProduct, service=rename_product, body=RenameIn, path="/rename"
)
products.command_route(DeleteProduct, service=delete_product, method="DELETE")
```

- A command with the resource's id field (`product_id`) acts on one
  aggregate: its route is `/{product_id}` plus `path`. Other commands act
  on the collection.
- Declaring the route checks the command. A body field the command
  lacks, or a required field that nothing gives, is a `TypeError` at
  import.
- A command field declared as a dataclass, such as a value object, or a
  tuple of one, gets the instances the body validated where the body's
  field is typed as that same class (or a tuple of it), as a `@use_case`'s
  `tuple[Address, ...]` is. They are not built again: pydantic builds
  them, and runs their `__post_init__`, but checks no markers
  (`MaxLength`) of theirs. Any other body field, a `list[...]`, and any
  field the command declares as `dict`, `Any`, `Mapping`, or anything
  else, comes as `model_dump()` gives it: a nested model or a dataclass
  as a dict. *Changed in 2.5.*
- The answer is the aggregate the service returns, in the resource's
  schema, with its `ETag`. A `POST` on the collection answers `201` with
  `Location`. OpenAPI documents the `ETag` and `Location`.
- Domain errors answer as everywhere: `NotFound` is `404`, `Conflict`
  `409`, and a `BusinessRuleViolation` `422`.

| `command_route` option | Default | What it does |
| --- | --- | --- |
| `service` | (required) | `service(command, uow)`: runs the command and returns the answer. |
| `body` | `None` | The request body's schema. None: no body. |
| `returns` | the resource's schema | Another schema for the answer (filled by `from_domain`, or from the result's attributes), without the fields the `FieldPolicy` hides. `None` answers `204`. |
| `method` | `"POST"` | `"POST"`, `"PUT"`, `"PATCH"`, or `"DELETE"`. A `DELETE` answers `204` by default. |
| `path` | `""` | The path after the resource's prefix, or after `/{product_id}`. |
| `action` | `add`, `delete`, or `change` | The permission `ModelActions` asks for: `action="publish"` needs `catalog.publish_product`. |
| `rule` | `None` | A rule of the route's own, added to the resource's. |
| `status_code` | (by the rules above) | The status of a success. |
| `name` | the command's name, in snake case | The route's name, and so its operation id. |
| `summary` | the command's docstring | The summary in OpenAPI. |
| `provide` | `None` | Fields given by FastAPI dependencies, such as the caller's id for an `owner_id`. The path wins over them, and they win over the body. |
| `body_limit` | the resource's | The most bytes a body may have ([uploads](uploads.md#body-limits-per-route)). |
| `retry` | `None` | `Retry()`: run the service again, whole, when its transaction loses a race. |
| `dependencies` | `()` | FastAPI dependencies of the route's own, such as a limit, `Depends(Throttle("50/day"))`. They run after the route's rules, so a request the rules refuse is not counted; but before the route reaches the aggregate (`404`, and `403` from a rule on it) and reads the body (`422`) and `If-Match` (`412`), so one refused there is. Its [twin](#authorizations-for-one-act) runs none of them. *New in 2.5.* |
| `before_rules` | `()` | FastAPI dependencies that run before the route's rules, after its body is read and parsed ([Dependencies before the rules](#dependencies-before-the-rules)). Its twin runs them too. *New in 2.8.* |

**Body limits.** `body_limit=` holds the route's bodies to that many
bytes, above or below `MAX_REQUEST_BODY_SIZE`, as they arrive.
`ResourceRouter(body_limits={"command": 16_384, "batch": 2_000_000})` sets
one for each kind of route: `create`, `update`, `command`, `batch`.

**Retries.** `retry=Retry()` runs the service again, whole, when its
transaction loses a race: a serialization failure of a `SERIALIZABLE`
unit of work, or a deadlock ([databases.md](databases.md#running-work-again)).
The reach check and `If-Match` are made again in each run's transaction.
Without it, a lost race answers 503 `database_busy`. Declare it for
services that change nothing outside their unit of work. It costs no
statement until a race is lost.

### Dependencies before the rules

*New in 2.8.* `dependencies` run
after the route's rules, so a limit there cannot count a request before
a rule reads its body: `DeviceSigned` reads it once the request names a
device of the caller's, where `DEVICES["CONTENT_DIGEST"]` is
`"optional"` and the request sends no digest, and a stolen session can
list the caller's devices. `before_rules` run first, in order, after the
resource's own and its tenant's, and count every request, those the
rules refuse included:

```python
phones.upload_route(
    AttachScan,
    service=attach_scan,
    field="scan",
    types=(CHECKED_PDF,),
    max_size=10_000_000,
    rule=DeviceSigned(),
    before_rules=[Depends(Throttle("30/hour"))],
)
```

The route's [twin](#authorizations-for-one-act) runs them too, where it
runs none of `dependencies`: it runs the rules they come before, and a
rule there may read the body as the act's does. A limit of acts alone
skips a twin's request with `asking_authorization(request)`. Nothing
that decides who may act goes there: a refusal there comes before the
rules, `429` before `401` ([ADR 0181](adr/0181-decide-a-body-limit-per-request-count-before-the-rules-hand-over-the-file-received-and-bound-the-xml-parsed.md)).

An upload route reads its body itself, once its rules and its reach
hold, so `before_rules` there come before any read of it. A command
route that takes a body is not so: FastAPI reads and parses its body
before any dependency runs, so `before_rules` there count a request
before its rules, but after its body arrived, within the route's
`body_limit`.

### Authorizations for one act

Some rules ask for an authorization of the act they guard, and refuse
the act until the client sends one: `StepUp(for_=...)` of
`domainkit.contrib.auth` is one
([ADR 0144](adr/0144-authorize-one-act-not-a-window-of-time.md)). A
command route whose rules hold such a rule gets a twin,
`POST <the route's path>/authorization`, where the client asks for it:

```python
products.command_route(
    DeleteProduct,
    service=delete_product,
    method="DELETE",
    rule=StepUp(for_="catalog.delete_product"),
)
```

```text
DELETE /products/42                        401 (the rule asks for an authorization)
POST   /products/42/authorization          201 (what the rule stored)
DELETE /products/42 (with it)              204
```

A limit of the act goes in the route's `dependencies`, so that asking
does not count against it:

```python
from fastapi import Depends

from domainkit.http.ratelimit import Throttle

invitations.command_route(
    SendInvitation,
    service=send_invitation,
    body=InvitationIn,
    rule=StepUp(for_="accounts.send_invitation"),
    dependencies=[Depends(Throttle("50/day", scope="invitations"))],
)
```

- The twin takes the route's path and body. It builds the command as
  the route does: the path's ids, the parent's field, `provide`, and the
  fields a `PatchModel` body sets, held to the field policy. Its body
  limit is the route's.
- Before it asks, it checks what the act would, with the asking rule
  passing: the route's other rules (`401` or `403`), the aggregate within
  the caller's reach (`404`), and the rules on the aggregate. The caller
  must have proved who they are. So no one asks for an authorization of
  an act they could not make.
- On an act on one aggregate it reads `If-Match` as the act does,
  optional (*new in 2.8*): after the checks above, an aggregate not at
  the version it names is `412`, and the unit of work it lends is held
  to it. It takes one tag, the version a page read: several, on one
  line or several, are `422` (`asked_versions`). A rule whose method takes `if_match` is given the
  header; `StepUp(for_=...)` and approvals' rules bind the act to it,
  and OpenAPI says so only for such a rule
  ([The version the page read](auth/actions.md#the-version-the-page-read)).
- It then awaits the rule's `ask_authorization(request, command)`, and
  answers `201` with what it returns, marked `Cache-Control: no-store`.
  It never runs the service, and writes nothing through its unit of
  work. A rule whose method also takes `uow` is given the route's unit
  of work, not yet open, to read what the caller is shown in (*new in
  2.5*): `StepUp(for_=...)` does, for an action's `show(command, uow)`.
- A rule may name a problem with the route's service
  (`service_problem(service)`), and the route is refused when declared
  (*new in 2.5*). `StepUp(for_=...)` refuses a service that does not
  spend that act's authorization.
- OpenAPI shows it as an operation of its own: the route's name and
  `_authorization` (`delete_product_authorization`), a summary, and the
  rule's `authorization_model()` as the `201` body. A twin of a route on
  the collection is matched before the routes of one item.
- The asking rule may be the resource's or the route's, alone or inside
  `&` at any depth. Inside `|` or `~` it cannot be decided, and two on
  one route would ask twice for one act: either raises
  `ImproperlyConfigured` when the route is declared.
- Where routes of one path ask by several methods (a `PATCH` and a
  `DELETE` of one item), each twin's path names its route's method, in
  lower case: `POST /products/42/authorization/patch` and
  `POST /products/42/authorization/delete`. Declaring the second moves
  the first's twin there. A route alone on its path keeps
  `<path>/authorization`. Two asking routes of one path and method, or a
  route of your own where a twin goes, raise `ImproperlyConfigured`.
  *New in 2.5* ([ADR 0162](adr/0162-twins-name-their-method-and-skip-the-acts-own-dependencies.md)).
- The twin runs none of the route's `dependencies`: a limit there counts
  acts, not the asks before them. It does run what builds the command,
  `provide`, and the resource's own dependencies (`ResourceRouter(...,
  dependencies=[...])`). Code among them that counts acts skips a twin's
  request: `asking_authorization(request)` (from `domainkit.resources`)
  is true there. A check of who may act belongs in a rule (`rule=`),
  which the twin asks too, never in `dependencies`. *New in 2.5.*
- A `@use_case` route gets its twin as a command route does. For an
  act, give `use_case` the copy `requires_authorization` makes, and
  leave the use case itself to the routes that ask nothing
  ([A use case as the act](auth/actions.md#a-use-case-as-the-act)).
- A route whose rules ask for nothing gets no twin, and nor do batches
  and generic writes. A resource's rule holds every route of the
  resource, reads included, so put an asking rule on the route
  (`rule=`).

A rule of your own asks for one by implementing `AsksAuthorization`
(`domainkit.http.access`): `authorization_model()` returns the model of
the `201` body, and `ask_authorization(request, command)` stores the
authorization for the request's caller and returns it. Declared
`ask_authorization(request, command, uow=None)`, it is given the
route's unit of work too.

### Files

*New in 2.5.* `upload_route(Command, service=..., field=..., types=...,
max_size=...)` takes a file. The command's `field` receives the
`StoredUpload` (`domainkit.storage`), stored before the service runs:

```python
from domainkit.http.uploads import CHECKED_PDF, OFFICE
from domainkit.storage import StoredUpload


class AttachContract(Command):
    """Attach the signed contract."""

    product_id: UUID
    contract: StoredUpload


products.upload_route(
    AttachContract,
    service=attach_contract,
    field="contract",
    types=(CHECKED_PDF, *OFFICE),
    max_size=10_000_000,
    storage="documents",
    directory="contracts",
    method="PUT",
    path="/contract",
)
```

```text
PUT /products/42/contract
X-Filename: %D8%B9%D9%82%D8%AF.pdf
Content-Type: application/pdf

%PDF-1.7 ...                                  200 (the product)
```

- The body is the file, and `X-Filename` its name, percent-encoded
  UTF-8. A phone that signs its requests (`DeviceSigned`) signs these
  bytes. A `multipart/form-data` form works too: its part named `field`
  holds the file.
- The file is received as `save_upload` receives one
  ([uploads](uploads.md#receiving-uploads)): its type from its content,
  its type's check ([Checking documents](uploads.md#checking-documents)),
  at most `max_size` bytes. A refusal is 400, 413, 415, or 422, and
  nothing is stored. The route's body limit is `max_size` and 64 KiB for
  a form, unless `body_limit` says otherwise.
- It checks what a command route checks, and before it reads a byte of
  the body: its rules (`401`, `403`), then, on one aggregate, the
  aggregate within the caller's reach (`404`), the rules on the
  aggregate (`403`), and `If-Match` (`412`, `428`). The command is built
  as a command route builds it: the path's ids, the parent's field, and
  `provide`; `digest` names a field that gets the file's SHA-256, in hex.
- The file is stored, in `storage` under `directory`, before the service
  runs, outside its transaction; the reach and `If-Match` are checked
  again in it. If the request fails and its unit of work did not commit,
  the file is deleted. Once it committed, a row may name the file, and
  it is kept, whatever fails after. A commit whose outcome is unknown
  (its answer lost, or cut off as it committed: `uow.in_doubt`) may have
  named it too: the file is kept, and the warning `Kept the upload <name>
  on the <storage> storage` names it, to check by hand. A file that
  cannot be deleted is logged with its name and storage too. *New in 2.9*
- The service may read the file as it was received
  ([The file received](#the-file-received)), not from the storage.
- The other options are `command_route`'s: `returns`, `method` (`POST`,
  `PUT`, or `PATCH`), `path`, `action`, `rule`, `status_code`, `name`,
  `summary`, `provide`, `body_limit`, `retry`, `dependencies` (*new in
  2.7*), which run on the act, never on its twin: a limit there counts
  acts, not asks; and `before_rules` (*new in 2.8*), which run before the
  rules, on the act and its twin. A `POST` on the collection answers
  `201` with `Location`.
- A route whose rule asks for an authorization gets its twin,
  `POST <path>/authorization`, which takes the same body. It checks the
  reach first, as the act does, then receives and checks the file,
  stores nothing, and builds the command with an upload whose `name` is
  empty. An `Action` binds an upload by its content (its type, size,
  file name and SHA-256), never by the name the storage gave it, which
  is new on every request: the authorization the twin asked for is spent
  on the same file, and refused for another. An upload inside a list, a
  set (its items sorted once their names are out), a mapping (whatever
  its keys, an enum's too) or a model of the command is bound the same
  way. Declare
  nothing about the upload; an `Action` that ignores its field binds no
  file at all.
- The act does not check again a file its twin's check took for the same
  caller (*new in 2.7*): the twin remembers the verdict, by the file's
  SHA-256 and its types' checks, for 30 minutes, in the default cache,
  signed (`receive_upload(remember=...)`,
  [uploads](uploads.md#bytes-and-streams)). A file checked once costs
  one process, not two. The file is still sent twice: the act stores the
  bytes it receives, which a phone signs.
- OpenAPI shows the body as `application/octet-stream` or a form, with
  the `X-Filename` header, on the route and its twin.

#### Bounds decided per request

*New in 2.7.* `types`, `max_size`, `storage` and `directory` may each be
a callable in place of a value, given the request and the aggregate the
route reached (`None` on the collection), sync or async. It runs once
the caller's reach and the rules on the aggregate hold, before a byte of
the body is read, on the act and on its twin alike:

```python
from domainkit.http.uploads import CHECKED_PDF, DOCX


def template_types(request: Request, template: Template) -> tuple[FileType, ...]:
    return (CHECKED_PDF,) if template.kind == "pdf" else (DOCX,)


async def template_size(request: Request, template: Template) -> int:
    return 20_000_000 if template.kind == "pdf" else 5_000_000


templates.upload_route(
    AttachFile,
    service=attach_file,
    field="file",
    types=template_types,
    max_size=template_size,
    body_limit=20_100_000,
    storage=lambda request, template: f"templates-{template.kind}",
    method="PUT",
    path="/file",
)
```

- A `max_size` decided per request needs `body_limit`: the most any
  request may send, file and form, held while the body arrives. Without
  it the route is refused when it is declared (`TypeError`).
- `body_limit` may be decided per request too (*new in 2.8*), so a bound
  that is a setting is read once, when the request comes, for the body
  and the file alike:

  ```python
  def upload_body(request: Request, template: Template) -> int:
      return settings.TEMPLATES_MAX_SIZE + 64 * 1024  # the file, and a form


  templates.upload_route(
      AttachFile,
      service=attach_file,
      field="file",
      types=template_types,
      max_size=lambda request, template: settings.TEMPLATES_MAX_SIZE,
      body_limit=upload_body,
      method="PUT",
      path="/file",
  )
  ```

  It is decided with the other bounds, before the route reads the body,
  and holds the body from then on: a `Content-Length` over it is 413
  before a byte is read, and a body sent without one is 413 as soon as
  it passes it. The file is never more than it, whatever `max_size`
  says. It is a whole number of bytes from 1 to 1 GiB, a route's most
  (`ValueError` otherwise, a 500). What reads the body before the route
  decides, a dependency of `before_rules` or `IdempotencyMiddleware`,
  reads it within the limit of a command route without `body_limit`:
  the resource's `body_limits["command"]`, or `MAX_REQUEST_BODY_SIZE`.
  A rule that says it reads the body (`reads_body`, as `DeviceSigned`
  does) is refused with such a limit when the route is declared
  (`ImproperlyConfigured`). Give such a route a limit decided before the
  rules ([below](#a-body-limit-decided-before-the-rules)), or a number,
  the most a request may send. A probe for a disconnect
  (`request.is_disconnected()`) made before the route decides is
  cancelled before it takes a message: the server keeps it for the read,
  and the limit is fixed by the read.
- Once one route of the application decides its limit, a declared
  length is checked before routing against 1 GiB, the most a route may
  decide, and each route's own limit at the first read of its body: a
  request to a route that never reads its body is answered as the route
  answers, where a declared length over every route's limit was 413
  before routing.
- What a callable decides is checked as a value given when declaring:
  no type, a size that is not a whole number of bytes, or a directory a
  storage refuses (`..`, `/root`), is a `ValueError`; a storage that is
  neither a `Storage` nor an alias, or a directory that is not text, a
  `TypeError`; an alias `STORAGES` lacks, `ImproperlyConfigured`. Each is
  a 500, a mistake of the route's, not the client's, raised before a
  byte of the body is read; so is a declared storage's or directory's.
- A type's check runs in a process of its own, without the settings
  ([Checks of your own](uploads.md#checks-of-your-own)): decide a limit
  that is a setting when the request comes, and give it to the check as
  its argument (`PdfCheck(max_pages=...)`).
- A twin's verdict is remembered with the checks it ran: an act whose
  callable decides other types checks the file again.

#### A body limit decided before the rules

*New in 2.10.* A rule that reads the body reads it before the route
decides its bounds: `DeviceSigned` does, to check what the device
signed. A body limit decided once the reach holds would come too late
for it, and is refused there. `BeforeRules` decides the limit before
the rules instead, from the request alone:

```python
from domainkit.resources import BeforeRules


def scan_body(request: Request) -> int:
    return settings.SCANS_MAX_SIZE + 64 * 1024  # the file, and a form


phones.upload_route(
    AttachScan,
    service=attach_scan,
    field="scan",
    types=(CHECKED_PDF,),
    max_size=lambda request, phone: settings.SCANS_MAX_SIZE,
    body_limit=BeforeRules(scan_body),
    rule=DeviceSigned(),
)
```

- `BeforeRules(decide)`: `decide` is given the request, sync or async,
  and returns the most bytes the body may have, 1 to 1 GiB (`ValueError`
  otherwise, a 500, the body unread). It runs first among the route's
  `before_rules`, after the resource's own dependencies and its
  tenant's, on the act and on its twin.
- It holds every read of the body from then on, the rules' included: a
  `Content-Length` over it is 413 before a byte is read, and a body sent
  without one is 413 as soon as it passes it, before the rule has read
  it all. It may be above the resource's `body_limits["command"]` or
  `MAX_REQUEST_BODY_SIZE`. The file is never more than it.
- No rule has run yet when it decides: decide from the settings and
  what the request says (its path, its headers), not from who sends it.
  It runs for every request, those the rules refuse included, as
  `before_rules` do.
- What reads the body before it reads it within the limit of a command
  route without `body_limit`: `IdempotencyMiddleware`, for a request with
  an `Idempotency-Key`; a dependency of the resource's own; and a FastAPI
  body parameter (`Body`, `Form`, `File`) declared in any of the route's
  dependencies, which FastAPI reads before any dependency runs. The file
  is still held to the limit decided. Where that read matters, keep the
  command limit at or above the largest limit decided.
- A limit decided once the reach holds, on a route whose rule reads the
  body, is still refused when the route is declared
  (`ImproperlyConfigured`), and the refusal names `BeforeRules`.

#### The file received

*New in 2.8.* The command holds the file as stored. A service that reads
what the file holds, a PDF's pages or a Word document's placeholders,
names a `received` parameter, and is given the file as the route
received it, a `ReceivedUpload` ([uploads](uploads.md#bytes-and-streams)),
so it reads no storage in its transaction:

```python
from domainkit.documents import placeholders
from domainkit.http.uploads import ReceivedUpload


async def attach_template(
    command: AttachTemplate, uow: TemplatesUnitOfWork, *, received: ReceivedUpload
) -> Template:
    data = await asyncio.to_thread(received.file.read)
    found = await asyncio.to_thread(placeholders, data)
    async with uow:
        template = await uow.templates.get(command.template_id)
        template.attach(command.file, found.names)
        await uow.commit()
    return template
```

- Its `file` is open at its start, each time the service runs, a
  run again by `retry` included. A file of more than 1 MiB is on disk:
  read it in a thread, as above. Reading a template's placeholders takes
  up to 340 MiB a call, and calls at once add up: bound how many run at
  once ([The XML parsed](documents.md#the-xml-parsed)).
- The route closes it once the service returns or raises. A task the
  service starts and leaves running finds it closed.
- It is the file the route stored: never store it again.
- A service without the parameter is called as before, with the command
  and the unit of work. The twin runs no service, and keeps nothing.

### Generic writes for reference data

For reference data, where generic edits are fine, `crud=True` adds three
routes:

| Route | What it does |
| --- | --- |
| `POST /products` | Creates one, from a `schema_for(..., write=True)` body. |
| `PATCH /products/{id}` | A partial change ([partial changes](#partial-changes)). |
| `DELETE /products/{id}` | Deletes one. |

They set fields directly, without the aggregate's methods. They record
`RecordCreated`, `RecordChanged` (with the fields that changed), and
`RecordDeleted`, so the audit trail still shows who changed what.
`writable=` names the fields the writes take, as Django forms name
theirs:

```python
products = ResourceRouter(
    "/products", ..., crud=True, writable=["name", "price", "color"]
)
```

- `writable=ALL_FIELDS` (from `domainkit.resources`) takes every plain
  field. `create=` and `update=` replace the two schemas.
- One of them is needed. A `crud=True` resource without `writable=`, or
  without both schemas, raises `TypeError` when it is built (since 0.11;
  0.10 warned and took every plain field). Otherwise a `balance`, an
  `owner_id`, or an `approved_at` added to the aggregate later would be
  settable by any caller allowed to write.
- `writable` may not name the id, the version, a relation, or the
  parent's field, which no write sets.
- `new_id=` makes ids (default: `uuid4`).
- Both writes keep to the resource's field policy (a create as a partial
  change does) and to the caller's `scope`. A create or a change that
  would leave the aggregate where the caller could not reach it is
  `403 permission_denied`, and nothing is written.

## Queries

A question beyond the listing's filters is a `Query` message, from
`domainkit.ddd`. A handler answers it, as a service runs a command:

```python
from domainkit.ddd import F, Query


class ProductsNamed(Query):
    """Products whose name holds some text."""

    text: Annotated[str, MaxLength(50)]


async def products_named(query: ProductsNamed, uow: CatalogUnitOfWork) -> list[Product]:
    async with uow:
        return await uow.products.filter(F("name").icontains(query.text))


products.query_route(ProductsNamed, handler=products_named, path="/named")
```

```text
GET /products/named?text=lamp
200 OK
{"items": [{"name": "Desk lamp", ...}, {"name": "Lamp", ...}]}
```

- The route builds the query from the query string, as
  `schema_for(query, write=True)` describes it: bounded as bodies are. An
  unknown parameter is `422`. A query with the resource's id field asks
  about one aggregate: `GET /products/{product_id}/<path>`.
- A handler that returns an aggregate answers as a read does, with its
  `ETag` (and `304`). One whose return annotation is a list, or another
  sequence, answers `{"items": [...]}`. Bound what it returns: the route
  sends all of it.
- What the caller may not reach or see is left out, even if the handler
  returns it. Each aggregate is checked against `scope` and the parent,
  and one out of reach is `404`. Field policies hide fields, as in reads.
  Filter in the handler all the same: the check is a safety net.
- `action` names the permission `ModelActions` asks for (`view`), and
  `rule` adds one, as for commands. `name` defaults to the query's name,
  in snake case. The path must start with `/`, and the handler must
  annotate what it returns (`TypeError`).

## Batches

`batch_route` runs one command for many items in one request:

```python
products.batch_route(RenameProduct, service=rename_product, body=RenameIn)
products.batch_route(
    DeleteProduct, service=delete_product, path="/deletions", action="delete"
)
```

`POST /products/batch` takes `{"items": [...], "atomic": false}`: 1 to
`max_items` (100) items, all validated before any runs. Each item is the
body's fields. For a command on one product, it also holds the
`product_id`, and may hold an `if_match` of its own. The answer is `207`,
a `BatchAnswer`: one `BatchResult` per item, in order, and how many
failed:

```json
{
  "results": [
    {"status": 200, "item": {"id": "f056550e-...", "version": 0, "name": "Big lamp", "stock": 5}, "etag": "W/\"0-a82e28cb78163705\""},
    {"status": 404, "error": {"code": "product_not_found", "detail": "Product 00000000-0000-0000-0000-000000000001 does not exist."}}
  ],
  "failed": 1
}
```

- **Each item answers as its own request would:** `200`, `201` with
  `location` for creations, `204` with `returns=None` and for
  `action="delete"`, or the error's status in a `BatchError` (`code` and
  `detail`). The caller's scope and the rules on objects apply to each
  item.
- **Without `atomic`,** each item is a use case of its own: a failure
  leaves the others done.
- **With `"atomic": true`,** all items run in one transaction. Each
  service's own commit waits for the batch's, and events are published
  once, at the end. The first failure undoes everything, and every other
  item answers `424 not_applied`.
- **Bugs are not results.** An unexpected exception in the first item
  fails the request with `500`, as it would for one item. Past the first
  item, without `atomic`, the items before it may have committed. So the
  batch answers `207`: that item `500 internal_error`, logged and
  reported, and the rest `424 not_applied`, not run. A money error
  (`MoneyError`) is the item's `422`, and a failed outgoing call its
  `502`, `503`, or `504`.
- **`action`** defaults to `change` for commands on one aggregate, and to
  `add` for the others. The route needs that permission once, for the
  batch.
- **`retry=Retry()`** runs work that lost a race again, as a command
  route's does. Without `atomic`, it runs the item that lost, with its
  reach check and `If-Match`. With `atomic`, it runs the whole batch
  again, in a new transaction, because the race ended the one it ran in.
  Once the runs are spent, the item answers `503 database_busy`, as
  without `retry`. Batches do not retry unless they say so.

`batch_route` takes `body`, `path` (`"/batch"`), `action`, `rule`,
`returns`, `max_items`, `name`, `summary`, `provide`, `body_limit`, and
`retry`.

## Exports

`exports=("csv", "ndjson")` lets the listing answer by `Accept`: JSON
pages, `text/csv`, or `application/x-ndjson` (one JSON document per
line). An export holds every match of the filters and the scope, in the
listing's order, read in chunks and streamed. `limit` and `cursor` do not
apply.

```text
GET /products  Accept: text/csv
200 OK
Content-Type: text/csv; charset=utf-8

id,version,name,stock
aae9f523-b4f4-4dcd-aabd-fd57fbc5e576,0,Desk lamp,0
f056550e-1f49-4cf9-b364-d942a15aded4,0,Big lamp,5
```

- More than `max_export` matches (10,000) is `422 export_too_large`
  (`ExportTooLarge`): narrow the export with filters. The count before
  the export stops at `max_export + 1` (since 2.9), so a filter that
  matches much is refused without counting every match.
- A request that accepts none of the offered types is `406
  not_acceptable` (`NotAcceptable`), with an `Accept` header that names
  them: `application/json, text/csv, application/x-ndjson`. Without
  `Accept`, the answer is JSON. Answers say `Vary: Accept`, for caches.
- CSV has the list's fields (`list_fields`, or all), less those the
  caller may not see. Nested values are JSON. A text cell that a
  spreadsheet would run as a formula gets an apostrophe first: one that
  starts with `=`, `+`, `-`, `@`, a tab, or a carriage return, also behind
  white space, control characters, or characters that show nothing, and
  in the full-width and small forms of those signs. Numbers, such as
  `-1.50`, stay as they are. What shows nothing is each format character
  (a directional mark, a soft hyphen) and each of Unicode's other default
  ignorable code points (a variation selector, a tag), as screens and
  `OfficeCheck` read them (since 2.8.1). LibreOffice 24.2 runs none of these
  as a formula; other spreadsheets were not all checked, and the file may
  be opened by any of them, so the apostrophe goes first all the same.
- NDJSON has one document per line. The separators that would split a
  line elsewhere (U+0085, U+2028, U+2029) are escaped, as JSON allows.

## Schemas from the domain

`schema_for` derives pydantic schemas from an aggregate's fields, so a
field is declared once:

```python
from domainkit.resources import schema_for

ProductOut = schema_for(Product)  # read: every field
ProductIn = schema_for(Product, write=True, include=["name", "price"])
ProductPatch = schema_for(Product, partial=True, include=["name", "price"])

ProductOut.from_domain(product)
```

| Domain field | Schema field |
| --- | --- |
| `ProductId` (a `NewType` over `UUID`), `Reference[CategoryId]` | A `UUID`. A reference is described as one. |
| A value object of one field, `ProductName(value: str)` | What it wraps, with its `MaxLength` as `maxLength`. |
| `Annotated[Decimal, Digits(12, 2)]` | An amount of 12 digits, 2 of them decimal, written as fixed-point text (`"1000.00"`, never `"1E+3"`). |
| `Money`, `Annotated[Money, Digits(12, 2)]` | `{"amount": "10.50", "currency": "USD"}`, the `Money` schema. A write schema refuses an amount its columns cannot hold (`money_digits`) ([money.md](money.md#over-http)). |
| `Currency` | Its code, `"USD"`. |
| An `Enum` | Its values. |
| A value object of several fields, a child entity, `list[Line]` | Nested schemas (`MoneyOut`, `LineOut`). |
| `set[str]` | A sorted list, so the representation is stable. |
| `X \| None` | The same, or null. |

| `schema_for` argument | Default | What it does |
| --- | --- | --- |
| `include` | every field but encrypted ones | The fields to keep, in this order. |
| `exclude` | `()` | Fields to drop. |
| `write` | `False` | Make a request body. |
| `partial` | `False` | Make a `PatchModel`, for a partial change. |
| `name` | `<Model>Out`, `<Model>In`, or `<Model>Patch` | The schema's name in OpenAPI. |
| `descriptions` | `None` | Text for each field, in OpenAPI. |

**A read schema** (a `DomainSchema`) lists every field, each one
required, and has `from_domain(obj)`, which fills it from an object:
value objects unwrapped, child entities nested. `domain_class` names the
class it was made from.

**A write schema** (`write=True`) leaves out `id`, `version`, and
`deleted_at` (and `tenant_id` of a `TenantOwned` aggregate). It takes the
domain's defaults, forbids unknown fields, takes only datetimes with a
time zone, and bounds integers to 64 bits.

**Secrets.** A field stored encrypted (`Annotated[str | None,
Encrypted(...)]`, a card number) is in a schema only where `include`
names it. A read schema, and the bodies that `crud=True` derives with
`writable=ALL_FIELDS`, leave it out. So no derived route serves it in
clear by default. Serve it on purpose with `schema_for(Card,
include=["id", "number"])`, or `writable=[..., "number"]`. A `FilterSet`
may filter such a field only where the resource's schema shows it: a
blind index answers whether a guessed value is stored, so a filter of one
is refused (`TypeError`) wherever the field is not shown. A field that
holds a secret (`Sensitive`, a password hash) is refused even when named.

`schema_for` raises `TypeError` for a field the model does not have, a
type that has no schema (exclude it, or write the schema yourself), or a
`Sensitive` field.

**From a request to the domain.** `to_domain(field, value)` turns a
request's value into what the field holds: value objects built, enums
found. The routes call it with the field's description, which domainkit
reads from the class. A value that does not fit is an `InvalidQuery`.

### A page at once

`present` turns one aggregate into the schema, and cannot wait. To show
what another domain holds, such as a member's name, a resource takes
`present_many` in its place (since 2.5). It is async, takes the
aggregates of one answer and the request, and returns one schema for
each, in their order:

```python
class MemberOut(BaseModel):
    id: UUID
    role: str
    name: str


async def members_out(members, request) -> list[MemberOut]:
    names = await people_named({each.user_id for each in members})  # one query
    return [
        MemberOut(id=each.id, role=each.role, name=names.get(each.user_id, ""))
        for each in members
    ]


members = ResourceRouter("/members", ..., schema=MemberOut, present_many=members_out)
```

- It is called once per answer: a page, an export's chunk, a query's
  items, or one aggregate for a read or a write. An empty page does not
  call it.
- The field policy, links, and the `ETag` apply to what it returns, as
  to `present`'s.
- One schema too few or too many, or one whose `id` is not its
  aggregate's (out of order), fails the request (`RuntimeError`): a name
  never lands on another's row. A schema without `id` is held to the
  count only.
- Give `present` or `present_many`, not both (`TypeError`).
- Show only what the caller may see of the other domain: the function
  has the request to decide.

A client that keeps the two apart asks the other domain once per page
instead, by id: the admin API's `GET /users` takes `id`, repeated, and
`GET /users/names` answers the same users by name alone, without their
groups and permissions (since 2.7,
[the admin API](auth/routes.md#the-admin-api)), by the fields
`AUTH.NAME_FIELDS` names (since 2.8).

### Partial changes

A `PatchModel` is a partial change, as RFC 7396's merge patch describes
it. Every field defaults to `UNSET`, and `changes()` holds only what the
request sent. So `{"note": null}` clears a note, and `{}` changes
nothing. A field takes `null` only if its type allows None. `PATCH`
routes accept `application/json` and `application/merge-patch+json`
(`MERGE_PATCH`).

```python
from pydantic import Field

from domainkit.ddd import UNSET, Command, Unset
from domainkit.resources import PatchModel


class ProductPatch(PatchModel):
    name: str = Field(max_length=200)
    note: str | None


class UpdateProduct(Command):
    product_id: UUID
    name: str | Unset = UNSET
    note: str | None | Unset = UNSET


products.command_route(
    UpdateProduct, service=update_product, body=ProductPatch, method="PATCH"
)
```

```python
ProductPatch.model_validate({}).changes()  # {}
ProductPatch.model_validate({"name": "Lamp"}).changes()  # {'name': 'Lamp'}
```

The command gets only the fields that were sent. The others stay
`UNSET`, so "leave it as it is" differs from "set it to None". The patch
model is the list of what may change, and unknown fields are `422`. A
`PatchModel` field must not declare a default of its own (`TypeError`).
In the service, test each field: `if command.name is not UNSET:`.

## Versions and conditional requests

An aggregate's answer carries a weak entity tag of its `version` and of
the representation sent: `ETag: W/"3-5d41402abc4b2a76"`. Two callers who
see different fields of one version (see [field policies](#field-policies))
get different tags.

- **`If-None-Match`** with the tag answers `304 Not Modified`, without a
  body, while the tag is current. Tags are compared weakly, as
  `@cache_page` compares its own: `"x"` and `W/"x"` match, and `*`
  matches anything.
- **`If-Match`** on a write names the version it changes. Another version
  answers `412 precondition_failed`, and nothing is changed. Tags are
  compared by the version they carry, so a client may also send `"3"`
  from a body's `version`. `*` takes any version. A tag this API did not
  make carries no version, so it never matches.
- **`ResourceRouter(..., require_if_match=True)`** refuses writes to one
  aggregate that send no `If-Match`, or `If-Match: *`, which names no
  version: `428 precondition_required` (`PreconditionRequired`).
- **`ResourceRouter(..., last_modified="updated_at")`** names a datetime
  field. Reading one aggregate then sends `Last-Modified`, and
  `If-Modified-Since` with that date, or a later one, answers `304`, to
  the second. Sent together, `If-None-Match` decides, as RFC 9110 says.
  A date that does not parse, or has no time zone, is ignored.

`entity_tag` and `expected_versions` make and read the tags:

```python
from domainkit.resources import entity_tag, expected_versions

entity_tag(3, b'{"name":"Lamp"}')  # 'W/"3-79df8fe3594b394d"'
expected_versions('W/"3-5d41402abc4b2a76", "4"')  # frozenset({3, 4})
expected_versions("*")  # None: any version
expected_versions('"abc"')  # frozenset(): no version, so nothing matches
```

A [twin](#authorizations-for-one-act) reads its `If-Match` with
`asked_versions` (*new in 2.8*): as `expected_versions`, but `None` for
an empty value too, and `422` for a list of several tags, however many,
since a page reads one version. A twin joins several `If-Match` lines
into one list first, as RFC 9110 reads them.

**How the check works.** The check needs nothing from the service. The
route hands the service a unit of work that holds the request's
expectation (`uow.expect(Order, order_id, versions)`, an `Expectation`).
Its repositories check the aggregate when they first hand it out, inside
the transaction that changes it: `get`, `find`, `filter`, `first`, and
`get_for_update` all do.

- A service that enters the unit of work again (it reads in one block,
  calls a payment provider, and writes in the next) is checked again in
  each new transaction. A change another client stored in between is
  refused. Only the version the unit of work's own commit produced is
  accepted.
- Only that unit of work is held to it. Another one that the service
  opens, to write an audit record first, say, commits as it would.

A route of your own does the same:

```python
from typing import Annotated

from fastapi import Header

from domainkit.resources import expected_versions


@router.post("/orders/{order_id}/cancel")  # With its rule, as every route.
async def cancel(order_id: OrderId, if_match: Annotated[str, Header()]) -> None:
    uow = OrdersUnitOfWork()
    with uow.expect(Order, order_id, expected_versions(if_match)):
        await services.cancel_order(CancelOrder(order_id=order_id), uow)
```

Until 0.12, the route could hold the expectation for the whole request
(`domainkit.ddd.preconditions.expect_version`), and every unit of work in
it was held to it. 1.0 removed that.

Optimistic locking (the `version` column) still refuses a change that
lands after the check, with `409 concurrent_update`. Two more cases:

- A service that never loads the aggregate through its repository cannot
  honour `If-Match`. Such a request fails with a `RuntimeError` rather
  than succeed unchecked: the unit of work refuses to commit before the
  aggregate is loaded, so nothing is written.
- A service that writes the aggregate through another unit of work than
  the one it was given is not held to `If-Match`. The request still
  fails, but that write stands. Change aggregates through the unit of
  work the route passes.

## Who may call it

Every route needs a rule. A resource's `rule` is a rule for all its
routes, or `ModelActions`, which asks for the model permission of each
route's action:

| Action | Routes | Permission (for `Product` in `catalog`) |
| --- | --- | --- |
| `view` | List, read, queries | `catalog.view_product` |
| `add` | Create, commands on the collection | `catalog.add_product` |
| `change` | Commands on one aggregate, partial changes | `catalog.change_product` |
| `delete` | Delete | `catalog.delete_product` |

So `POST /products/{id}/rename` needs `catalog.change_product`, where
`ModelPermissions` would ask for `add`, as for any `POST`.

- A command's `action=` names another permission: `action="publish"`
  needs `catalog.publish_product`. `rule=` adds a rule of the route's
  own.
- `ModelActions(Product, IsAdminUser())` asks for staff too: the rules
  given come first, then the model permission.
- `ModelActions` takes the class, its content type as text
  (`"catalog.product"`), or a function that returns the class (resolved
  on each request, as for a swappable user model).
- Each permission is a `HasModelPermission(action, model)`;
  `.permission()` gives its key. It checks the credentials' scopes too,
  so a token limited to that key passes it.
- `ModelActions(Limit, everywhere=True)` counts only the keys a user
  holds where no tenant is served: a member's role in the tenant served
  does not, for a resource over rows that belong to no tenant
  ([rows of no tenant](tenancy.md#rows-of-no-tenant)).
  `HasModelPermission` takes `everywhere=` too.
- A rule of your own that depends on the action is any object with
  `rules_for(action)`, which returns the rules to check: the `Actions`
  protocol.

A resource without a rule declares none, so it fails the deny-by-default
check: `check` lists its routes, and staging and production refuse to
start. Pass `dependencies=[Public]` for a resource anyone may call.

### A resource whose items are tenants

A resource's items may be tenants: companies, organisations, projects,
each with members (since 2.5). `tenant=` takes a `PathTenant`, and
serves each route whose path holds its parameter as that tenant, before
the route's rules ([a tenant from the path](tenancy.md#a-tenant-from-the-path)):

```python
from uuid import UUID

from starlette.requests import HTTPConnection

from domainkit.ddd import Criteria, F
from domainkit.http.access import IsAuthenticated
from domainkit.resources import MemberActions, ResourceRouter
from domainkit.tenancy import PathTenant


async def companies_of_caller(request: HTTPConnection) -> Criteria:
    user = request.scope["user"]
    ids = await member_tenants(user) if user.is_authenticated else []
    return F("id").in_([UUID(each) for each in ids])


companies = ResourceRouter(
    "/companies",
    ...,
    tenant=PathTenant("company_id"),
    rule=MemberActions(Company, outside=IsAuthenticated()),
    scope=companies_of_caller,
)
companies.command_route(RenameCompany, service=rename, body=RenameIn, path="/rename")
companies.command_route(FoundCompany, service=found, body=FoundIn, action="add")
```

`member_tenants` is the project's `TENANCY.MEMBER_TENANTS` function: the
ids of the companies the user belongs to.

- `GET /companies/{company_id}` and `POST /companies/{company_id}/rename`
  are served as the company. A caller who does not belong to it gets
  `404`, an anonymous one `401`, as `PathTenant` answers.
- `GET /companies` and `POST /companies` hold no `{company_id}`: they
  serve no tenant. On a router, `PathTenant` would refuse them
  (`tenancy.E001`).
- No member rule narrows a list: the `scope` keeps each caller to the
  companies they belong to, in lists, queries, and exports. Under
  `MemberActions(outside=<a rule>)`, a resource without one is refused
  with `TypeError`; a public directory gives a scope that returns
  `None`, on purpose.
- Each command, use case, or batch on the collection names its action
  (`action="add"` to create a company; `"change"`, `"delete"`, or one of
  its own otherwise), or is refused with `TypeError`. A `POST` there is
  `add` by default, which `outside` may let anyone do: a command that
  names another company in its body would change it for them.
- A batch of commands on single companies is refused with `TypeError`:
  each item names a company the batch is not served as. Declare a
  command route on one company instead.
- The parameter may be the resource's id, its parent's, or one of its
  prefix (`/orgs/{org_id}/projects`, where every route is the org's). One
  that no route holds is refused with `TypeError`.
- A resource whose own id is the tenant lives at a prefix that names no
  parameter: under `/orgs/{org_id}`, its collection would serve no
  tenant, and is refused with `TypeError`.
- A nested resource takes its parent's tenant. One of its own is refused
  with `TypeError`: the routes that name only the parent's id would
  serve no tenant.
- Every other route of the resource is served as no tenant
  (`serve_no_tenant`), even with `TenantMiddleware` too: the tenant a
  header or a subdomain names decides nothing on the collection. So no
  member's keys count there, `ModelActions` and a `FieldPolicy` count
  what the caller holds everywhere, and `current_tenant()` is None.
- A command on the collection is not held to the `scope`: the item its
  body names is the service's to load, and the route cannot know it. Its
  action's rule decides (`outside`'s, or `Actions`'), and the service
  checks the item it loads. To act on one company as its member, put the
  command under `/{company_id}`, where the scope and the member's rule
  hold.
- `fetch(request, uow, id_)` from a route of your own on the collection
  serves no tenant and asks no member's key: it reaches the companies in
  the caller's `scope`, and asks only the rules that look at objects. To
  act as a member of one company, put the route under `/{company_id}`.
- A router included into the resource is served as the tenant its
  prefix names: `companies.include_router(files,
  prefix="/{company_id}/files")`. One included under another prefix, and
  a Starlette route, WebSocket route, mount, or host, which take no
  dependency, are refused with `TypeError`: on the resource, and inside
  a router it includes, at any depth, as FastAPI serves those without the
  include's dependencies. FastAPI includes a router as it is, so one
  added to it after the include is served too: `check`, and the
  application as it starts, refuse it (`tenancy.E003`). Finish a router
  before you include it.
- Routes of your own on the resource (`@companies.get(...)`, a
  WebSocket) are served alike, by their path.
- The id is read as the resource's id type. A UUID is the tenant's id in
  lowercase, however the path writes it, and a value that is not one is
  `422`. A route of your own that reads the parameter itself reads it as
  it declares it: declare it as a `UUID` too, or take the tenant's id
  from the `PathTenant` (`Annotated[str, Depends(companies.tenant)]`).

`MemberActions(Company)` asks for the permission of each route's action,
as `ModelActions` does, among the keys the caller holds as a member of
the tenant served (`TENANCY.MEMBER_PERMISSIONS`), and no others. A member
who is also staff acts there with their role, not with their staff
permissions ([members' rules](tenancy.md#members-permissions)). On a
route that serves no tenant it refuses, unless `outside=` says who may
call it:

- A rule, such as `IsAuthenticated()`, decides the `view` and `add`
  routes there: the list, queries on the collection, creation. A route
  of another action there, such as a command on the collection declared
  with `action="change"` (a merge), is refused. Give such a command its
  action: a `POST` on the collection is `add` by default.
- `Actions`, such as `ModelActions(Company)`, name the rules of every
  action there themselves, as for a resource without tenants.
- Credentials limited to scopes pass `outside` only where its rules
  check scopes. On a tenant's routes, the member's rule checks them.

A resource given `outside=` and no `tenant=` is refused with `TypeError`:
its items would serve no tenant either, and `outside` would rule them.

An action of the domain's own whose key names no model's permission (an
invitation posted to a company, which needs another domain's
`organizations.add_membership`) is named whole by `keys`, by action
(since 2.7):

```python
companies = ResourceRouter(
    "/companies",
    ...,
    tenant=PathTenant("company_id"),
    rule=MemberActions(
        Company,
        outside=IsAuthenticated(),
        keys={"invite": "organizations.add_membership"},
    ),
    scope=companies_of_caller,
)
companies.command_route(Invite, service=invite, path="/invite", action="invite")
```

Without `keys`, the route would ask `companies.invite_company`, which no
model declares. The other actions keep their model's keys.

- Once `keys` is given, a route of an action other than `view`, `add`,
  `change`, or `delete` that `keys` does not name is refused when it is
  declared (`ImproperlyConfigured`, naming the closest it does name), so
  a misspelt action never asks a key no one holds.
- `ModelActions` takes `keys` alike. A key that is not
  `<domain>.<codename>` is refused (`ValueError`).
- `outside=ModelActions(...)` names its own keys, as it names its own
  rules: it takes none of the member rule's. Give it `keys` too where the
  collection serves such an action.

### Rules on objects, and scope

Rules that look at objects (`HasObjectPermissions`, or a rule of your own
with `has_object_permission`) are asked about the aggregate itself,
before it is read or a command runs on it. That holds for the resource's
rules, and for a route's own `rule=` (of a command, a batch, a query, or
the admin's). A query that answers several aggregates leaves out those
the rules refuse.

`scope` narrows what a caller may reach to criteria of their own:

```python
from starlette.requests import HTTPConnection

from domainkit.ddd import Criteria, F


async def own_orders(request: HTTPConnection) -> Criteria | None:
    user = current_user(request)
    return None if user.is_superuser else F("customer_id") == user.id


orders = ResourceRouter("/orders", ..., scope=own_orders)
```

What is out of scope is left out of pages, so counts and cursors never
reveal it. It is `404` for reads and commands, as if it did not exist.

**How the check runs.** A command or a query on one aggregate is checked
before its service runs.

- When only the scope or the parent decide, the check and the service
  share one unit of work. The aggregate is read within reach, and the
  service's first `async with uow:` block runs in the same transaction.
  Every block's exit is the unit of work's own. So what a block did not
  commit is rolled back as before, and a later block runs in a
  transaction of its own: a change a service gave up in one block never
  commits with the next.
- When a rule looks at the aggregate, the check has a unit of work of its
  own, closed before the rule runs, and the service another. So a rule
  may query the database itself (object permission grants) without
  waiting for a second connection while the request holds one. The
  service is then held to the version the rule saw. An aggregate another
  request changed in between is `409 concurrent_update` (or the request's
  `412` with `If-Match`), never written past the rule.
- Generic writes do the same. An atomic batch asks the rules about every
  item before its transaction begins. The first item refused undoes the
  batch, and each aggregate is held to its version at the first item that
  acts on it.

The rules are the core's (`domainkit.http.access`). A resource needs no
`domainkit.contrib.auth`, only a middleware that puts who sent the
request in `scope["user"]`
([another authentication](auth/routes.md#another-authentication)).

## Field policies

A `FieldPolicy` says who sees and who changes each field, as the user
model's `EDITABLE_FIELDS` and `READ_ONLY_FIELDS` do for the auth admin:

```python
from domainkit.resources import FieldPolicy

policy = FieldPolicy(
    read_only=["created_at"],
    visible={"cost": "billing.view_cost"},
    editable={"discount": "billing.change_discount"},
)
invoices = ResourceRouter(
    "/invoices", ..., policy=policy, crud=True, writable=["amount", "cost", "discount"]
)
```

- A field in `visible` is shown only to callers granted its permission.
  Others get the representation without the key. OpenAPI marks the field
  optional, and names the permission.
- A partial change, or a generic create, that sets a field in `editable`
  needs its permission: `403 field_not_editable` (`FieldNotEditable`).
  One that sets a field in `visible` needs that permission too: what a
  caller may not see, they may not overwrite blind.
- A `read_only` field is in neither body. It keeps its default, so give
  it one (or a `create=` schema of your own).
- Task commands are not partial changes: what a command changes is the
  domain's business.
- Fields left out of the schema are neither shown nor changed at all,
  which keeps protected fields protected.

## Nested resources and links

`parent=` nests a resource under another, for aggregates that refer to
it by id:

```python
reviews = ResourceRouter(
    "/reviews", model=Review, parent=products, parent_field="product_id"
)
router.include_router(products)
router.include_router(reviews)
```

- Its routes are `/products/{product_id}/reviews` and
  `/products/{product_id}/reviews/{review_id}`.
- Lists hold the parent's reviews only. Another product's review is
  `404`. A command's `product_id` comes from the path.
- With `crud=True`, `product_id` is in neither request body. A review is
  created under the product its path names, and no change moves it to
  another.
- `links=True` adds `links` to each representation: `self`, and `parent`
  for a nested one, as paths of this API.

A prefix may hold path parameters of its own, not a parent's:
`ResourceRouter("/companies/{company_id}/templates", ...)`. A create's
`Location` and the links take them from the request, percent-encoded:
`/companies/7/templates/<id>`. So they take those of a router the
resource is included in, `APIRouter(prefix="/orgs/{org}")`. They are the
parameters of the first route of the read route's name in the
application: give two resources of one model a `name_prefix`. A resource
in a mounted application fills its own prefix's only. Read them in
`scope`, a rule, or `provide` (`request.path_params["company_id"]`).

## Routes of your own on a resource

`ResourceRouter.fetch(request, uow, id_, action="view")` loads an
aggregate as the resource's own routes would. Out of scope is `404`, and
rules that look at objects are asked. It works on resources that are not
nested:

```python
@products.get("/{product_id}/label")
async def label(request: Request, product_id: UUID, uow: UowDep) -> LabelOut:
    product = await products.fetch(request, uow, product_id)
    return LabelOut(text=product.name.upper())
```

`products.operations` lists each route as an `Operation`: its `name`,
`method`, `path`, `action`, `rules`, `summary`, `detail` (whether it acts
on one aggregate), `body`, and `parameters`. The admin and generic
clients read it:

```text
('GET', '/products', 'list_products', 'view')
('GET', '/products/{product_id}', 'read_product', 'view')
('POST', '/products/{product_id}/restock', 'restock', 'change')
('GET', '/products/named', 'products_named', 'view')
('POST', '/products/batch', 'rename_product_batch', 'change')
```

## Options

| `ResourceRouter` argument | Default | What it does |
| --- | --- | --- |
| `prefix` | (required) | The path, starting with `/` and not ending with one. |
| `model` | (required) | The aggregate class, or a function that returns it (for a swappable model; then `schema`, `basename`, and `id_type` are required). |
| `unit_of_work` | (required) | A FastAPI dependency that returns a unit of work. A class is called without arguments. |
| `repository` | (required) | Picks the repository from a unit of work: `lambda uow: uow.products`. |
| `schema` | `schema_for(model)` | The read schema. |
| `present` | `schema.from_domain` | Turns an aggregate into the schema. |
| `present_many` | `None` | Async, in `present`'s place: turns the aggregates of one answer into their schemas, in one call ([a page at once](#a-page-at-once)). |
| `rule` | `None` | A rule for every route, `ModelActions`, or `MemberActions`. |
| `filters` | `None` | A `FilterSet`, or a dependency that returns a `Selection` or criteria. |
| `pagination` | `Pagination()` | How the listing pages ([pages over HTTP](queries.md#pages-over-http)). |
| `page_schema` | (derived) | Replaces the page's schema. |
| `lookup` | `<basename>_id` | The name of the id in the path. |
| `id_type` | the aggregate's id type | The type of the id in the path. |
| `basename`, `plural` | from the class name | The names in routes and paths. |
| `parent`, `parent_field` | `None` | Nest under another resource. |
| `tenant` | the parent's, or `None` | A `PathTenant`: the routes whose path holds its parameter are served as that tenant ([items that are tenants](#a-resource-whose-items-are-tenants)). |
| `scope` | `None` | Criteria a caller may reach ([scope](#rules-on-objects-and-scope)). |
| `policy` | `None` | A `FieldPolicy`. |
| `require_if_match` | `False` | Writes to one aggregate need `If-Match` (428 without). |
| `last_modified` | `None` | A datetime field for `Last-Modified`. |
| `links` | `False` | Add `links` to each representation. |
| `crud`, `writable`, `create`, `update`, `new_id` | `False`, `None`, `None`, `None`, `uuid4` | Generic writes ([generic writes](#generic-writes-for-reference-data)). |
| `not_found` | the repository's `not_found` | The error for an id that names nothing. |
| `list_fields` | `None` | Keep only these fields in lists and exports. |
| `name_prefix` | `""` | Starts every route's name, so two resources of one model can live in one application. |
| `exports`, `max_export` | `()`, 10,000 | Export formats, and the most rows an export holds. |
| `body_limits` | `None` | Body limits per kind of route. |
| `**options` | | `APIRouter`'s options: `tags`, `dependencies`. |

## Generators

`startdomain catalog --aggregate Product` writes a domain in four files,
one per layer. `check` holds each file to its layer, as it does
packages:

| File | Layer | Holds |
| --- | --- | --- |
| `domain.py` | domain | The aggregate, its rules, errors, and events; the repository and unit-of-work ports. Plain Python. |
| `controllers.py` | application | The commands, and the use cases that run them. Other domains use this domain through it. |
| `models.py` | infrastructure | The table (`table_for`), the repository, the unit of work, and `start_mappers()`, which the domain's default `DomainConfig` calls: no `apps.py`. |
| `routes.py` | api | The resource, and who may call it. |

It writes an aggregate that has a name, use cases to create and rename
it, and tests of the use cases and the API. In a project laid out as
`init` makes it, it also installs the domain and mounts its router.
`--manual` prints those two steps instead.

**Other layouts.**

- `startdomain --layered` writes a package per layer instead (`domain/`,
  `application/`, `infrastructure/`, `api/`), on the whole kit: an
  aggregate with a value object, a collection of child entities, and a
  reference by id; its filters, `PATCH`, commands, and `DELETE`; and
  tests at every layer.
- `startdomain catalog --aggregate Product --minimal` writes a smaller
  layered domain (`--minimal` implies `--layered`): an aggregate with a
  name that must not be blank, commands to create and rename it, its
  table, its resource, an in-memory unit of work, and two tests. That is
  13 files where the full one has 15, and 15 names from domainkit where
  it has 29. Its events, and their handlers' modules, are left out until
  a generator needs them.
- `startdomain --user` writes the project's own user model and its table,
  before the first `migrate` ([users](auth/users.md)).

**Growing a domain.** The generators below grow both layouts, every
layer at once, with tests. In a four-file domain, `addcommand` writes the
method and its event in `domain.py`, a `@use_case` in `controllers.py`,
its route in `routes.py`, and a test on `in_memory(...)`. `addquery`
writes the query and its handler in `controllers.py`, and its route.
`addevent` writes the event in `domain.py`, and a `@handles` handler in
`controllers.py`.

```bash
python manage.py startdomain catalog --aggregate Product --layered
python manage.py startaggregate catalog Category \
    --field title:str:100 --field parent:ref:Category? --field price:decimal
python manage.py addcommand catalog Product PublishProduct --field at:datetime
python manage.py addquery catalog Product ProductsByOwner --by owner_id
python manage.py addevent catalog Product ProductRestocked --field quantity:int
python manage.py makemigrations -m "catalog" && python manage.py migrate
```

| Generator | Writes |
| --- | --- |
| `startaggregate` | The aggregate, its id, its not-found error, and its repository port; created and deleted events; create and delete commands and use cases; its table (`table_for`), mapping, and repository; its resource; an in-memory repository; `test_<plural>.py`. |
| `addcommand` | The aggregate's method, which records the event (the rule and the change are yours to write); the command and its use case; `POST /<plural>/{id}/<verb>` with its body; `test_<command>.py`. |
| `addquery` | A `Query` and its handler (`application/queries.py` in a layered domain), finding the first 100 aggregates whose `--by` fields equal the query's, typed as the aggregate types them; `GET /<plural>/<path>` (`query_route`); `test_<query>.py`. |
| `addevent` | The event, carrying the aggregate's id; a durable handler, subscribed in `apps.py` (`--no-handler` leaves it out); `test_on_<event>.py`. |

**Event names.** `addcommand` names the event after the aggregate and the
verb's past participle:

- `-ed` added: `PublishProduct` gives `ProductPublished`;
- a last consonant doubled where English doubles it: `ShipOrder` gives
  `OrderShipped`;
- a common irregular verb's: `PayOrder` gives `OrderPaid`;
- others, by `--event`: `SpellWord --event WordSpelt`.

**Field kinds.** Each `--field` is `name:kind[:options]`, with `?` for a
field that may be None:

| Kind | In the aggregate | Column | In request bodies |
| --- | --- | --- | --- |
| `str[:length]` (200) | `Annotated[str, MaxLength(length)]` | `String(length)` | At most `length` characters |
| `text` | `str` | `Text` | |
| `int` | `int` | `BigInteger` | 64 bits |
| `bool`, `float` | `bool`, `float` | `Boolean`, `Double` | |
| `decimal[:precision:scale]` (12, 2) | `Annotated[Decimal, Digits(p, s)]` | `ExactDecimal(p, s)` | Those digits |
| `money[:precision:scale]` (18, 4) | `Annotated[Money, Digits(p, s)]` | `MoneyColumns`: `<name>`, `ExactDecimal(p, s)`, and `<name>_currency` | `{"amount": "1.50", "currency": "USD"}` |
| `date`, `datetime` | `date`, `datetime` | `Date`, `UTCDateTime` | A datetime has a time zone |
| `uuid` | `UUID` | `Uuid` | |
| `ref:Aggregate` | `Reference[AggregateId]` | `Uuid`, indexed | A UUID |

**Money that moves.** A money field that may not be None also moves
between aggregates, as [the payment recipe](recipes/payments.md) moves
it. `--field balance:money` on `Wallet` writes:

- `transfer_balance(to=, amount=)` and `receive_balance` on the
  aggregate. They refuse an overdraft (`InsufficientWalletBalance`),
  nothing, a transfer to itself (`InvalidWalletTransfer`), and another
  currency (`CurrencyMismatch`);
- the event `WalletBalanceTransferred`;
- the command `TransferWalletBalance` and its use case. The use case
  locks both rows in one statement, by ascending id
  (`get_many_for_update`), and runs the whole work again when it loses a
  race (`uow.run(work, retry=RACES)`, with
  `Retry(attempts=5, retry_on=(ConcurrentUpdate,))`);
- `POST /wallets/{wallet_id}/transfer-balance`;
- `test_transfer_wallet_balance.py`, a lost race included.

**Your own field kinds.** `register_field_type("email", FieldType(...))`,
from `domainkit.management.generators`, adds a kind, for code that runs
the generators itself: a project command that registers its kinds, then
calls `call_command("startaggregate", ...)`.

**What they guarantee.**

- The generators check every name: snake_case or CamelCase identifiers,
  no keywords, nothing already defined.
- They find what they edit by its syntax tree, and plan every edit in
  memory. A file changed beyond recognition is refused, and then nothing
  is written.
- They format what they wrote with the project's ruff, which `init` puts
  in its dev group. Where ruff is not installed, they log a warning and
  leave the code for `ruff check --fix . && ruff format .`.
- `fakes.py` gains `new_<name>()`, a valid aggregate, and
  `new_<name>_request()`, a valid body to create one. The tests of later
  generators start from them.
- What they generate passes ruff, `mypy --strict`, and its own tests, as
  the generators' own test checks.

[Commands](reference/commands.md) lists every option of each generator.

## The generic admin

`domainkit.contrib.admin` is Django's `admin.site.register` without HTML.
Register an aggregate, and the admin serves it, with `GET /meta`
describing everything for a generic client to render:

```python
from domainkit.contrib import admin

admin.register(
    Product,
    unit_of_work=get_unit_of_work,
    repository=lambda uow: uow.products,
    list_display=["name", "archived"],
    search=["name"],
    ordering=["name"],
    commands=[
        admin.command(
            ArchiveProduct, service=services.archive_product, path="/archive"
        ),
        admin.command(
            RenameProduct,
            service=services.rename_product,
            body=RenameIn,
            path="/rename",
        ),
    ],
    queries=[
        admin.query(
            ProductsByOwner, handler=queries.products_by_owner, path="/by-owner"
        )
    ],
)

# config/urls.py, after the modules that register:
router.include_router(admin.site.router, prefix="/admin")
```

| Route | Answer |
| --- | --- |
| `GET /admin/catalog/products` | A page. Each item has `id`, the `list_display` fields, and `links`. `filters=` takes a `FilterSet`. `search=` and `ordering=` make one, so each field of `ordering=` needs a bound, as in any `FilterSet` (`MaxLength` on text, `Digits` on amounts). |
| `GET /admin/catalog/products/{product_id}` | The product, with its `ETag`. |
| `POST /admin/catalog/products/{product_id}/archive` | A command, run by its service. `If-Match` makes it conditional. |
| `GET /admin/catalog/products/{product_id}/history` | The audit trail's entries about the product, newest first, with what each changed ([contrib.md](contrib/audit.md#history)), when `domainkit.contrib.audit` is installed. |
| `GET /admin/meta` | Each model the caller may view: its paths, the JSON Schema of an item without the fields the caller may not see, the listed fields, filters, ordering, search, the commands and queries the caller may run (with their bodies' JSON Schemas), and its history's path if the caller may read it. |

- **Access.** Every route needs staff (`is_staff`) or a superuser, with
  credentials not limited to scopes, and the model permission of its
  action: `catalog.view_product` to list, read, and query; `change` for
  commands (or a command's `action=`); and `audit.view_history` too for
  histories. `/meta` needs staff. With multi-tenancy, a model's
  permission counts as the staff member holds it everywhere, unless the
  model is `TenantOwned`, whose rows are the tenant's served; so does
  `audit.view_history`, always
  ([rows of no tenant](tenancy.md#rows-of-no-tenant)).
- **Actions are commands** through application services. The admin never
  sets fields itself, so the domain's rules and events hold.
- **Options.** `schema=`, `present=`, `policy=`, `scope=`, and
  `pagination=` are the resource's. `label=` names the content type when
  the class is not mapped in an installed domain; it is refused when it
  names another model than the class's own. `history_field=` names the
  events' field that holds the id (`product_id`).
- **Names.** Route names start with `admin_<domain>_`, so the admin and
  the domain's own API can serve one model in one application. A
  resource's `list_fields=` and `name_prefix=` are the options the admin
  uses.
- **Users, groups, and permissions** keep their own admin at
  `/admin/auth`, whose grant ceilings and last-superuser lock the generic
  admin could not keep. Registering them is refused, and a swapped user
  model (any `AbstractBaseUser`) too, whatever its label. Their listings
  and reads there are resources too, with tags ([auth.md](auth.md)). No
  schema that `schema_for` derives shows a password hash: name it in
  `exclude=`.
- **`/meta`** describes the fields each caller may see, and the nested
  schemas those fields use, no others.
- **Register before the router is mounted:** from the modules your routes
  import, or `DomainConfig.ready()`. A later `register` fails.

## Errors and refusals

| What goes wrong | Status and `code` | Where |
| --- | --- | --- |
| A body, path, or query value breaks its schema | 422 `validation_error` | Parameters |
| A command field breaks its markers | 422 `invalid_field` | The command |
| No user / no permission | 401 `not_authenticated` / 403 `permission_denied` | Rules |
| Out of scope, or not there | 404 `not_found` (or the repository's code) | Reach, the use case |
| `If-Match` names another version | 412 `precondition_failed` | Reach |
| No `If-Match` with `require_if_match` | 428 `precondition_required` | Reach |
| Changed since the rule looked, or since the load | 409 `concurrent_update` | Reach, the commit |
| A field the caller may not change | 403 `field_not_editable` | Generic writes |
| An export of more than `max_export` rows | 422 `export_too_large` | Exports |
| `Accept` names no offered type | 406 `not_acceptable` | Exports |
| An item of an atomic batch after a failure | 424 `not_applied` | Batches |
| A batch item that raised a bug | 500 `internal_error` | Batches |
| A declaration that does not fit the model | `TypeError` at import | Declaring routes |

[Request flow](request-flow.md) shows where each one happens, in order.

## Common mistakes

- **A resource with no rule.** Staging and production refuse to start.
  Give it `rule=ModelActions(Model)`, or `dependencies=[Public]` on
  purpose.
- **`crud=True` without `writable=`.** It raises `TypeError`. Name the
  fields the writes may set; a field added later stays safe.
- **Committing inside a `@use_case`.** The use case commits for you.
  Write `await uow.commit()` only in a service you write by hand.
- **A query handler that returns everything.** The route sends all of
  it. Bound it: `paginate(..., limit=100)`.
- **Writing in a second unit of work.** `If-Match` holds only the unit
  of work the route passes. Change the aggregate through that one.

## See also

- [Validation](validation.md): the markers that bound bodies.
- [Queries](queries.md): `FilterSet`, `Pagination`, and the lookups.
- [Request flow](request-flow.md): every stage of a request, in order.
- [Users and permissions](auth.md): users, groups, and model permissions.
- [Messaging](contrib/messaging.md): durable handlers and deferred tasks.
- [Problems](reference/problems.md): every error code.
- [Commands](reference/commands.md): every generator and its options.
