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
    ordering_fields = ("name",)
    default_ordering = ("name",)
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
`filters` is a `FilterSet`, or any dependency that returns a `Query` or
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

For reference data, where generic edits are fine, `crud=True` adds
`POST /products` (a `schema_for(..., write=True)` body), `PATCH
/products/{id}` (a partial change), and `DELETE /products/{id}`. They set
fields directly, without the aggregate's methods, and record
`RecordCreated`, `RecordChanged` (with the fields that changed), and
`RecordDeleted`, so the audit trail still shows who changed what.
`create=` and `update=` replace the schemas; `new_id=` makes ids
(`uuid4`).

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
| `Annotated[Decimal, Digits(12, 2)]` | an amount of 12 digits, 2 decimal |
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

The check needs nothing from the service. The route holds the request's
expectation (`domainkit.ddd.preconditions.expect_version`), and the
repository checks the aggregate when it first hands it out, inside the
transaction that changes it: `get`, `find`, `filter`, `first`, and
`get_for_update` all do. Optimistic locking (the `version` column) still
refuses a change that lands after the check, with `409
concurrent_update`. A service that never loads the aggregate through its
repository cannot honour `If-Match`; such a request fails with a
`RuntimeError` rather than succeed unchecked.

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
it is read or a command runs on it. `scope` narrows what a caller may reach
to criteria of their own:

```python
async def own_orders(request: HTTPConnection) -> Criteria | None:
    user = current_user(request)
    return None if user.is_superuser else F("customer_id") == user.id


orders = ResourceRouter("/orders", ..., scope=own_orders)
```

What is out of scope is left out of pages, so counts and cursors never
reveal it, and is `404` for reads and commands, as if it did not exist.

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
invoices = ResourceRouter("/invoices", ..., policy=policy, crud=True)
```

- A field in `visible` is shown only to callers granted its permission;
  others get the representation without the key. OpenAPI marks it
  optional and names the permission.
- A partial change that sets a field in `editable` needs its permission
  (`403 field_not_editable`), and one that sets a `read_only` field is
  refused. Task commands are not partial changes: what a command changes
  is the domain's business.
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
`product_id` comes from the path. `links=True` adds `links` to each
representation: `self`, and `parent` for a nested one, as paths of this
API.

## The admin APIs

The auth admin's user and group listings and reads are resources, so they
answer with tags too; their writes stay commands of their own, which keep
the grant ceilings and the last-superuser lock ([auth.md](auth.md)).
