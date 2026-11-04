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
| `Personal(of=...)`, `Counted` | Personal data about someone; a collection the audit trail counts, not lists. `fields_about(holder, values, subject)` picks what a copy holds about one person. | [Privacy](privacy.md), [Audit](contrib/audit.md) |
| `Reference[XId]` | The id of another aggregate. | [Many-to-many](databases.md#many-to-many) |

**Events.** An aggregate records events with `self.record(event)`. The
unit of work publishes them after a successful commit, and never for work
that did not commit. `pending_events` shows what is recorded and not yet
published; `pull_events()` takes them. An event has an `event_id` and an
`occurred_at` of its own. Carry plain values in events (ids, text,
numbers), so they stay valid messages after the model changes.

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


@handles(ProductRestocked, durable=True)
async def tell_the_warehouse(event: ProductRestocked) -> None:
    ...
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

```python
print(restock)
print(restock.command(product_id=product.id, quantity=2))
```

```text
<use case restock (Restock)>
Restock(product_id=UUID('3374d5e3-6b53-40cc-9be7-fdf68c20a508'), quantity=2)
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
  (`ExportTooLarge`): narrow the export with filters.
- A request that accepts none of the offered types is `406
  not_acceptable` (`NotAcceptable`), with an `Accept` header that names
  them: `application/json, text/csv, application/x-ndjson`. Without
  `Accept`, the answer is JSON. Answers say `Vary: Accept`, for caches.
- CSV has the list's fields (`list_fields`, or all), less those the
  caller may not see. Nested values are JSON. A text cell that a
  spreadsheet would run as a formula gets an apostrophe first: one that
  starts with `=`, `+`, `-`, `@`, a tab, or a carriage return, also behind
  spaces of any kind or invisible characters, and in the full-width and
  small forms of those signs. Numbers, such as `-1.50`, stay as they are.
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
- A rule of your own that depends on the action is any object with
  `rules_for(action)`, which returns the rules to check: the `Actions`
  protocol.

A resource without a rule declares none, so it fails the deny-by-default
check: `check` lists its routes, and staging and production refuse to
start. Pass `dependencies=[Public]` for a resource anyone may call.

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
| `rule` | `None` | A rule for every route, or `ModelActions`. |
| `filters` | `None` | A `FilterSet`, or a dependency that returns a `Selection` or criteria. |
| `pagination` | `Pagination()` | How the listing pages ([pages over HTTP](queries.md#pages-over-http)). |
| `page_schema` | (derived) | Replaces the page's schema. |
| `lookup` | `<basename>_id` | The name of the id in the path. |
| `id_type` | the aggregate's id type | The type of the id in the path. |
| `basename`, `plural` | from the class name | The names in routes and paths. |
| `parent`, `parent_field` | `None` | Nest under another resource. |
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
        admin.command(ArchiveProduct, service=services.archive_product, path="/archive"),
        admin.command(
            RenameProduct, service=services.rename_product, body=RenameIn, path="/rename"
        ),
    ],
    queries=[
        admin.query(ProductsByOwner, handler=queries.products_by_owner, path="/by-owner")
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
  histories. `/meta` needs staff.
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
