# Resources: aggregates over REST

`domainkit.resources` is what Django REST framework's viewsets, routers,
and serializers are to Django, for task-based commands. A `ResourceRouter`
lists and reads one kind of aggregate, and runs commands on it through the
application's services:

```python
from domainkit.http import Filter, FilterSet, Pagination
from domainkit.resources import ModelActions, ResourceRouter, schema_for


class ProductFilters(FilterSet):
    model = Product
    archived = Filter()
    ordering_fields = ("name",)  # text a client orders by needs a MaxLength
    search_fields = ("name",)


ProductIn = schema_for(Product, write=True, include=["name"])

products = ResourceRouter(
    "/products",
    model=Product,
    unit_of_work=get_unit_of_work,  # a dependency; tests override it
    repository=lambda uow: uow.products,
    filters=ProductFilters,
    pagination=Pagination(max_limit=100),
    rule=ModelActions(Product),  # view, add, change, delete
    tags=["catalog"],
)
products.command_route(CreateProduct, service=services.create_product, body=ProductIn)
products.command_route(
    RenameProduct, service=services.rename_product, body=RenameIn, path="/rename"
)
products.command_route(DeleteProduct, service=services.delete_product, method="DELETE")

router.include_router(products, prefix="/api/v1/catalog")
```

| Route | Answer |
| --- | --- |
| `GET /products` | A page: `{"items": [...], "next_cursor": ...}`, filtered, ordered, and searched by the `FilterSet`, paged by `Pagination` ([queries.md](queries.md)). |
| `GET /products/{product_id}` | The product, with an `ETag`; `If-None-Match` answers `304`. |
| `POST /products` | `CreateProduct`, built from the body, run by the service; `201` with `Location` and `ETag`. |
| `POST /products/{product_id}/rename` | `RenameProduct` on one product; the id comes from the path. `If-Match` makes it conditional. |
| `DELETE /products/{product_id}` | `204`. |

`model` is the aggregate class. `unit_of_work` is a FastAPI dependency that
returns a unit of work (a class is called without arguments), and
`repository` picks the repository from it. The read schema defaults to
`schema_for(model)`; pass `schema=` (and `present=`, a function from the
aggregate to it) for one of your own. The id's path parameter is
`<basename>_id`, `product_id` here, typed as the aggregate's ids are.
`filters` is a `FilterSet`, or any dependency that returns a `Selection` or
criteria. Every route, its parameters, and the errors it may answer with
are in OpenAPI.

## Commands

Task-based commands are the norm: each request is one use case, and the
domain decides what changes. `command_route(Command, service=..., body=...)`
builds the command from the body's fields and the path's ids, and awaits
`service(command, uow)`, which opens the unit of work, changes the
aggregate, and commits, as the generated services do:

```python
async def rename_product(command: RenameProduct, uow: CatalogUnitOfWork) -> Product:
    async with uow:
        product = await uow.products.get(ProductId(command.product_id))
        product.rename(ProductName(command.name))
        await uow.commit()
    return product
```

- A command with the resource's id field (`product_id`) acts on one
  aggregate: its route is `/{product_id}` plus `path`. Others act on the
  collection.
- Declaring the route checks the command: a body field the command lacks,
  or a required field nothing gives, is a `TypeError` at import.
- The answer is the aggregate the service returns, in the resource's
  schema, with its `ETag`; a `POST` on the collection answers `201` with
  `Location`. `returns=OtherSchema` answers with another schema
  (`from_domain`, or the result's attributes); `returns=None`, and a
  `DELETE` by default, answer `204`.
- Domain errors answer as everywhere: `NotFound` is `404`, `Conflict`
  `409`, a `BusinessRuleViolation` `422`.
- `summary` defaults to the command's docstring; `name` (the route's name,
  hence its operation id) to the command's, in snake case.
- `body_limit=` holds the route's bodies to that many bytes, above or
  below `MAX_REQUEST_BODY_SIZE`, as they arrive; `ResourceRouter(body_limits=
  {"command": 16_384, "batch": 2_000_000})` sets one per kind of route
  (`create`, `update`, `command`, `batch`) ([uploads.md](uploads.md#body-limits-per-route)).
- `retry=Retry()` runs the service again, whole, when its transaction
  loses a race: a serialization failure of a `SERIALIZABLE` unit of work,
  or a deadlock ([databases.md](databases.md#running-work-again)). The
  reach check and `If-Match` are made again in each run's transaction.
  Without it a lost race answers 503 `database_busy`, as before. Declare
  it for services that change nothing outside their unit of work; it
  costs no statement until a race is lost.

For reference data, where generic edits are fine, `crud=True` adds
`POST /products` (a `schema_for(..., write=True)` body), `PATCH
/products/{id}` (a partial change), and `DELETE /products/{id}`. They set
fields directly, without the aggregate's methods, and record
`RecordCreated`, `RecordChanged` (with the fields that changed), and
`RecordDeleted`, so the audit trail still shows who changed what.
`writable=` names the fields the writes take, as Django forms name theirs:

```python
products = ResourceRouter(
    "/products", ..., crud=True, writable=["name", "price", "color"]
)
```

`writable=ALL_FIELDS` (from `domainkit.resources`) takes every plain
field, and `create=` and `update=` replace the schemas. One of them is
needed: a `crud=True` resource without `writable=`, or without both
schemas, raises `TypeError` when it is built (since 0.11; 0.10 warned and
took every plain field), since a `balance`, an `owner_id`, or an
`approved_at` added to the aggregate later would be settable by any
caller allowed to write. `writable` may not name
the id, the version, a relation, or the parent's field, which no write
sets. `new_id=` makes ids (`uuid4`). Both writes keep to the resource's field policy (a create, as
a partial change does) and to the caller's `scope`: a create or a change
that would leave the aggregate where the caller could not reach it is
`403 permission_denied`, and nothing is written.

## Queries

A question beyond the listing's filters is a `Query` message (from
`domainkit.ddd`), answered by a handler as a command is by a service:

```python
class ProductsByOwner(Query):
    """The first products of one owner."""

    owner_id: UUID


async def products_by_owner(
    query: ProductsByOwner, uow: CatalogUnitOfWork
) -> list[Product]:
    async with uow:
        page = await uow.products.paginate(F("owner_id") == query.owner_id, limit=100)
    return page.items


products.query_route(
    ProductsByOwner, handler=queries.products_by_owner, path="/by-owner"
)
```

- `GET /products/by-owner?owner_id=...` builds the query from the query
  string, described by `schema_for(query, write=True)`: bounded as bodies
  are, and an unknown parameter is `422`. A query with the resource's id
  field asks about one aggregate: `GET /products/{product_id}/<path>`.
- A handler that returns an aggregate answers as a read does, with its
  `ETag` (and `304`). One whose return annotation is a list, or another
  sequence, answers `{"items": [...]}`. Bound what it returns: the route
  sends all of it.
- What the caller may not reach or see is left out even if the handler
  returns it: each aggregate is checked against `scope` and the parent,
  and one out of reach is `404`; field policies hide fields as in reads.
  Filter in the handler all the same; the check is a safety net.
- `action` names the permission `ModelActions` asks for (`view`), and
  `rule` adds one, as for commands; `name` defaults to the query's, in
  snake case.

## Batches

`batch_route` runs one command for many items in one request:

```python
products.batch_route(RenameProduct, service=services.rename_product, body=RenameIn)
products.batch_route(
    DeleteProduct, service=services.delete_product, path="/deletions", action="delete"
)
```

`POST /products/batch` takes `{"items": [...], "atomic": false}`: 1 to
`max_items` (100) items, all validated before any runs. Each item is the
body's fields and, for a command on one product, its `product_id` and an
`if_match` of its own. The answer is `207`, one result per item, in order:

```json
{
  "results": [
    {"status": 200, "item": {"id": "...", "name": "New"}, "etag": "W/\"2-...\""},
    {"status": 404, "error": {"code": "not_found", "detail": "..."}}
  ],
  "failed": 1
}
```

- Each item answers as its own request would: `200` (`201` with
  `location` for creations, `204` with `returns=None` and for
  `action="delete"`), or the error's status, code, and detail. The
  caller's scope and rules on objects apply to each item.
- Without `atomic`, each item is a use case of its own: a failure leaves
  the others done. With `"atomic": true`, all run in one transaction:
  each service's own commit waits for the batch's, events are published
  once at the end, and the first failure undoes everything, every other
  item answering `424 not_applied`.
- Bugs are not results: an unexpected exception fails the request with
  `500`, as it would for one item.
- `action` defaults to `change` for commands on one aggregate and `add`
  for the others; the route needs that permission once, for the batch.
- `retry=Retry()` runs work that lost a race again, as a command route's
  does: without `atomic`, the item that lost, with its reach check and
  `If-Match`; with `atomic`, the whole batch, in a new transaction, since
  the race ended the one it ran in. Once the runs are spent, the item
  answers `503 database_busy`, as without `retry`. Batches do not retry
  unless they say so.

## Exports

`exports=("csv", "ndjson")` lets the listing answer by `Accept`: JSON
pages, `text/csv`, or `application/x-ndjson` (one JSON document per
line). An export holds every match of the filters and the scope, in the
listing's order, read in chunks and streamed; `limit` and `cursor` do not
apply.

- More than `max_export` matches (10,000) is `422 export_too_large`:
  narrow it with filters.
- A request that accepts none of the offered types is `406
  not_acceptable`, with `Accept` naming them; without `Accept`, JSON.
  Answers say `Vary: Accept`, for caches.
- CSV has the list's fields (`list_fields`, or all), less those the
  caller may not see; nested values are JSON. A text cell that a
  spreadsheet would run as a formula (starting with `=`, `+`, `-`, `@`,
  a tab, or a carriage return, also behind spaces of any kind or
  invisible characters, and in the full-width and small forms of those
  signs) gets an apostrophe first; numbers, such as `-1.50`, stay as they
  are.
- NDJSON has one document per line: the separators that would split a
  line elsewhere (U+0085, U+2028, U+2029) are escaped, as JSON allows.

## Schemas from the domain

`schema_for` derives pydantic schemas from an aggregate's fields, so a
field is declared once:

```python
ProductOut = schema_for(Product)  # read: every field
ProductIn = schema_for(Product, write=True, include=["name", "price"])
ProductPatch = schema_for(Product, partial=True, include=["name", "price"])

ProductOut.from_domain(product)
```

| Domain field | Schema field |
| --- | --- |
| `ProductId` (a `NewType` over `UUID`), `Reference[CategoryId]` | a `UUID`; a reference is described as one |
| a single-field value object, `ProductName(value: str)` | what it wraps, with its `MaxLength` as `maxLength` |
| `Annotated[Decimal, Digits(12, 2)]` | an amount of 12 digits, 2 decimal, written as fixed-point text (`"1000.00"`, never `"1E+3"`) |
| an `Enum` | its values |
| a value object of several fields, a child entity, `list[Line]` | nested schemas (`MoneyOut`, `LineOut`) |
| `set[str]` | a sorted list, so the representation is stable |
| `X | None` | the same, or null |

A read schema lists every field, required, and has `from_domain`. A write
schema (`write=True`) leaves out `id`, `version`, and `deleted_at`, takes
the domain's defaults, forbids unknown fields, takes only datetimes with a
time zone, and bounds integers to 64 bits. `include` keeps fields, in its
order; `exclude` drops some; `name` names the schema; `descriptions`
documents fields.

### Partial changes

A `PatchModel` is a partial change, as RFC 7396's merge patch describes:
every field defaults to `UNSET`, and `changes()` holds only what the
request sent, so `{"note": null}` clears a note, and `{}` changes nothing.
A field takes `null` only if its type allows None. `PATCH` routes accept
`application/json` and `application/merge-patch+json`.

```python
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
    UpdateProduct, service=services.update_product, body=ProductPatch, method="PATCH"
)
```

The command gets only the fields that were sent; the others stay `UNSET`.
The patch model is the allowlist of what may change, and unknown fields are
`422`.

## Versions and conditional requests

An aggregate's answer carries a weak entity tag of its `version` and of
the representation sent: `ETag: W/"3-5d41402abc4b2a76"`. Two callers who
see different fields of one version (see field policies) get different
tags.

- `If-None-Match` with the tag answers `304 Not Modified`, without a body,
  while the tag is current. Tags are compared weakly, as `@cache_page`
  compares its own: `"x"` and `W/"x"` match, and `*` matches anything.
- `If-Match` on a write names the version it changes. Another version
  answers `412 precondition_failed`, and nothing is changed. Tags are
  compared by the version they carry, so a client may also send `"3"`
  from a body's `version`. `*` takes any version. A tag this API did not
  make carries no version, so it never matches.
- `ResourceRouter(..., require_if_match=True)` refuses writes to one
  aggregate that send no `If-Match`: `428 precondition_required`.

The check needs nothing from the service. The route hands the service a
unit of work that holds the request's expectation
(`uow.expect(Order, order_id, versions)`), and its repositories check the
aggregate when they first hand it out, inside the transaction that changes
it: `get`, `find`, `filter`, `first`, and `get_for_update` all do. Only
that unit of work is held to it: another one the service opens, to write
an audit record first, say, commits as it would. A route of your own does
the same:

```python
@router.post("/orders/{order_id}/cancel")  # With its rule, as every route.
async def cancel(order_id: OrderId, if_match: Annotated[str, Header()]) -> None:
    uow = OrdersUnitOfWork()
    with uow.expect(Order, order_id, expected_versions(if_match)):
        await services.cancel_order(CancelOrder(order_id=order_id), uow)
```

Until 0.12 the route held the expectation for the whole request
(`domainkit.ddd.preconditions.expect_version`), and every unit of work in
it was held to it; that still works, deprecated, until 0.13.

Optimistic locking (the `version` column) still refuses a change that
lands after the check, with `409 concurrent_update`. A service that never
loads the aggregate through its repository cannot honour `If-Match`; such
a request fails with a `RuntimeError` rather than succeed unchecked: the
unit of work refuses to commit before the aggregate is loaded, so nothing
is written. A service that writes the aggregate through another unit of
work than the one it was given is not held to `If-Match`: the request
still fails, but that write stands. Change aggregates through the unit of
work the route passes.

## Who may call it

Every route needs the resource's `rule`, a rule for all of them, or
`ModelActions`, which asks for Django's model permission of each route's
action: `view` to list and read, `add` to create, `change` for commands and
partial changes, `delete` to delete. So `POST /products/{id}/rename` needs
`catalog.change_product`, where `ModelPermissions` would ask for `add`, as
for any `POST`. A command's `action=` names another permission
(`action="publish"` needs `catalog.publish_product`), and `rule=` adds a
rule of its own. `ModelActions(Product, IsAdminUser())` asks for staff too.

A resource without a rule declares none, so it fails the deny-by-default
check: `check` lists its routes, and staging and production refuse to
start. Pass `dependencies=[Public]` for a resource anyone may call.

Rules that look at objects (`HasObjectPermissions`, a rule of your own
with `has_object_permission`) are asked about the aggregate itself before
it is read or a command runs on it: the resource's rules, and the route's
own `rule=` (of a command, a batch, a query, or the admin's). A query
that answers several aggregates leaves out those the rules refuse.
`scope` narrows what a caller may reach to criteria of their own:

```python
async def own_orders(request: HTTPConnection) -> Criteria | None:
    user = current_user(request)
    return None if user.is_superuser else F("customer_id") == user.id


orders = ResourceRouter("/orders", ..., scope=own_orders)
```

What is out of scope is left out of pages, so counts and cursors never
reveal it, and is `404` for reads and commands, as if it did not exist.

A command or a query on one aggregate is checked before its service runs.
When only the scope or the parent decide, the check and the service share
one unit of work: the aggregate is read within reach, and the service's
first `async with uow:` block runs in the same transaction. Every block's
exit is the unit of work's own, so what a block did not commit is rolled
back as before, and a later block runs in a transaction of its own: a
change a service gave up in one block never commits with the next. When a
rule
looks at the aggregate, the check has a unit of work of its own, closed
before the rule runs, and the service another.

The rules are the core's (`domainkit.http.access`): a resource needs no
`domainkit.contrib.auth`, only a middleware that puts who sent the request
in `scope["user"]` ([Another authentication](auth/routes.md#another-authentication)).

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

- A field in `visible` is shown only to callers granted its permission;
  others get the representation without the key. OpenAPI marks it
  optional and names the permission.
- A partial change, or a generic create, that sets a field in
  `editable` needs its permission (`403 field_not_editable`); one that
  sets a field in `visible` needs that permission too, since what a
  caller may not see they may not overwrite blind. A `read_only` field
  is in neither body: it keeps its default, so give one (or a `create=`
  schema of your own). Task commands are not partial changes: what a
  command changes is the domain's business.
- Fields left out of the schema are neither shown nor changed at all,
  which keeps protected fields protected.

## Nested resources and links

`parent=` nests a resource under another, for aggregates that reference
it by id:

```python
reviews = ResourceRouter(
    "/reviews", model=Review, ..., parent=products, parent_field="product_id"
)
router.include_router(products)
router.include_router(reviews)
```

Its routes are `/products/{product_id}/reviews` and
`/products/{product_id}/reviews/{review_id}`: lists hold the parent's
reviews only, another product's review is `404`, and a command's
`product_id` comes from the path. With `crud=True`, `product_id` is in
neither request body: a review is created under the product its path
names, and no change moves it to another. `links=True` adds `links` to each
representation: `self`, and `parent` for a nested one, as paths of this
API.

## Generators

`startdomain` writes a domain on this kit: an aggregate with a value
object, a collection of child entities, and an id-only reference; its
filters, `PATCH`, commands, and `DELETE`; and tests at every layer. In a
project laid out as `init` makes it, it also installs the domain and
mounts its router. `startdomain catalog --aggregate Product --minimal`
writes a smaller one, in the same four layers: an aggregate with a name
that must not be blank, commands to create and rename it, its table, its
resource, an in-memory unit of work, and two tests; 13 files of the
package where the full one has 15, and 15 names from domainkit where it
has 29. Its events and their handlers' modules are left out until a
generator needs them. Then the generators add to either, every layer at
once, with tests:

```bash
python manage.py startdomain catalog --aggregate Product
python manage.py startaggregate catalog Category \
    --field title:str:100 --field parent:ref:Category? --field price:decimal
python manage.py addcommand catalog Product PublishProduct --field at:datetime
python manage.py addquery catalog Product ProductsByOwner --by owner_id
python manage.py addevent catalog Product ProductRestocked --field quantity:int
python manage.py makemigrations -m "catalog" && python manage.py migrate
```

| Generator | Writes |
| --- | --- |
| `startaggregate` | The aggregate, its id, not-found error, and repository port; created and deleted events; create and delete commands and use cases; its table (`table_for`), mapping, and repository; its resource; an in-memory repository; `test_<plural>.py`. |
| `addcommand` | The aggregate's method, which records the event (the rule and the change are yours to write); the command and its use case; `POST /<plural>/{id}/<verb>` with its body; `test_<command>.py`. The event is the aggregate and the verb with `-ed` added (`ProductPublished`); name irregular ones with `--event` (`ShipProduct --event ProductShipped`). |
| `addquery` | A `Query` and its handler in `application/queries.py`, finding the first 100 aggregates whose `--by` fields equal the query's, typed as the aggregate types them; `GET /<plural>/<path>` (`query_route`); `test_<query>.py`. |
| `addevent` | The event, carrying the aggregate's id; a durable handler, subscribed in `apps.py` (`--no-handler` leaves it out); `test_on_<event>.py`. |

Each `--field` is `name:kind[:options]`, with `?` for a field that may be
None:

| Kind | In the aggregate | Column | In request bodies |
| --- | --- | --- | --- |
| `str[:length]` (200) | `Annotated[str, MaxLength(length)]` | `String(length)` | at most `length` characters |
| `text` | `str` | `Text` | |
| `int` | `int` | `BigInteger` | 64 bits |
| `bool`, `float` | `bool`, `float` | `Boolean`, `Double` | |
| `decimal[:precision:scale]` (12, 2) | `Annotated[Decimal, Digits(p, s)]` | `ExactDecimal(p, s)` | those digits |
| `date`, `datetime` | `date`, `datetime` | `Date`, `UTCDateTime` | a datetime has a time zone |
| `uuid` | `UUID` | `Uuid` | |
| `ref:Aggregate` | `Reference[AggregateId]` | `Uuid`, indexed | a UUID |

`register_field_type("email", FieldType(...))`, from
`domainkit.management.generators`, adds a kind for code that runs the
generators itself, such as a project command that registers its kinds and
then calls `call_command("startaggregate", ...)`.

The generators check every name (snake_case or CamelCase identifiers, no
keywords, nothing already defined), find what they edit by its syntax
tree, and plan every edit in memory: a file changed beyond recognition is
refused, and then nothing is written. They format what they wrote with
the project's ruff, which `init` puts in its dev group; where ruff is not
installed, they log a warning and leave the code for
`ruff check --fix . && ruff format .`. `fakes.py` gains `new_<name>()`, a valid aggregate, and
`new_<name>_request()`, a valid body to create one, which the tests of
later generators start from. What they generate passes ruff, `mypy
--strict`, and its own tests, as the generators' own test checks.

## The generic admin

`domainkit.contrib.admin` is Django's `admin.site.register` without HTML:
register an aggregate, and the admin serves it, with `GET /meta`
describing everything for a generic client to render.

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
| `GET /admin/catalog/products` | A page; each item has `id`, the `list_display` fields, and `links`. `filters=` takes a `FilterSet`; `search=` and `ordering=` make one, so each field of `ordering=` needs a bound, as any `FilterSet` does (`MaxLength` on text, `Digits` on amounts). |
| `GET /admin/catalog/products/{product_id}` | The product, with its `ETag`. |
| `POST /admin/catalog/products/{product_id}/archive` | A command, run by its service; `If-Match` makes it conditional. |
| `GET /admin/catalog/products/{product_id}/history` | The audit trail's entries about the product, newest first, with what each changed ([contrib.md](contrib/audit.md#history)), when `domainkit.contrib.audit` is installed. |
| `GET /admin/meta` | Each model the caller may view: its paths, the JSON Schema of an item without the fields the caller may not see, the listed fields, filters, ordering, search, the commands and queries the caller may run (with their bodies' JSON Schemas), and its history's path if the caller may read it. |

- Every route needs staff (`is_staff`) or a superuser, with credentials
  not limited to scopes, and the model permission of its action:
  `catalog.view_product` to list, read, and query, `change` for commands
  (or a command's `action=`), and `audit.view_history` too for histories.
  `/meta` needs staff.
- Actions are commands through application services: the admin never
  sets fields itself, so the domain's rules and events hold.
- `schema=`, `present=`, `policy=`, `scope=`, and `pagination=` are the
  resource's; `label=` names the content type when the class is not
  mapped in an installed domain, and is refused when it names another
  model than the class's own; `history_field=` names the events' field
  that holds the id (`product_id`).
- Route names start with `admin_<domain>_`, so the admin and the domain's
  own API can serve one model in one application.
- Users, groups, and permissions keep their own admin at `/admin/auth`,
  whose grant ceilings and last-superuser lock the generic admin could not
  keep: registering them is refused, a swapped user model (any
  `AbstractBaseUser`) too, whatever its label. Their listings and reads
  there are resources too, with tags ([auth.md](auth.md)). No schema
  `schema_for` derives shows a password hash: name it in `exclude=`.
- `/meta` describes the fields each caller may see, and the nested
  schemas those fields use, no others.
- Register before the router is mounted (from the modules your routes
  import, or `DomainConfig.ready()`); a later `register` fails.

`ResourceRouter.fetch(request, uow, id)` loads an aggregate as the
resource's own routes would, out of scope being `404`, for routes of your
own on a resource; `list_fields=` and `name_prefix=` are the options the
admin uses.
