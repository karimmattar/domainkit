# Validation

A field's limits are written once, on the field, and every way in keeps
them: the object itself, the request body, and the column.

```python
from typing import Annotated

from domainkit.ddd import (
    AggregateRoot,
    BusinessRuleViolation,
    MaxLength,
    MinLength,
    Pattern,
    Range,
)

Sku = Annotated[str, MinLength(3), MaxLength(12), Pattern("[A-Z0-9-]+")]


class Product(AggregateRoot[ProductId]):
    sku: Sku
    name: Annotated[str, MaxLength(100)]
    stock: Annotated[int, Range(min=0)] = 0
    discontinued: bool = False

    def clean(self) -> None:
        if self.discontinued and self.stock:
            raise BusinessRuleViolation("A discontinued product holds no stock.")
```

| Marker | Allows |
| --- | --- |
| `MaxLength(n)` | text of at most `n` characters |
| `MinLength(n)` | text of at least `n` characters |
| `Pattern(regex)` | text the pattern matches from end to end; write `[0-9]` for digits, since `\d` also matches other scripts' digits |
| `Range(min=..., max=...)` | a number from `min` to `max`, both included; either may be left out |
| `IPAddressFormat(version=None)` | an IPv4 or IPv6 address, or only one of them with `version=4` or `6` |

Four field types come ready, as Django's fields do: `Email` (at most 254
characters, a local part, `@`, and a domain of two labels or more), `URL`
(`http` or `https`, at most 2,048), `Slug` (Latin letters, digits, `-`,
and `_`, at most 50), and `IPAddress`. Each is an `Annotated[str, ...]` of
the markers above, so it is checked where they are, and sized in its
column:

```python
from domainkit.ddd import URL, AggregateRoot, Email, Slug


class Site(AggregateRoot[SiteId]):
    contact: Email
    slug: Annotated[Slug, Unique()]
    home: URL | None = None
```

An `Email` that matches may still bounce: only sending to it shows that.

## Where they are checked

- **The object.** An entity or aggregate checks a field whenever it is set,
  when it is made and on every later change: `product.stock = -1` raises
  `InvalidField`. Value objects, commands, and events check their fields
  once, when they are made. A `@use_case`'s parameters are its command's
  fields, so `await restock(uow, quantity=0)` is refused before the
  function runs.
- **The request.** `schema_for` and `ResourceRouter.use_case` build bodies
  with the same limits, so a bad body is a 422 before any use case runs,
  and OpenAPI shows them.
- **The database.** `table_for` sizes text columns from `MaxLength`.

`None` is never checked: `Annotated[str, MaxLength(3)] | None` allows it.

## Rules across fields: `clean()`

`clean()` is Django's hook for rules that involve more than one field. It
runs when the entity is made, and on every aggregate a unit of work saw,
just before it commits. A use case may change fields one at a time and
pass through a state that breaks the rule; only what is committed must
keep it. Raise a `DomainError` subclass, as `BusinessRuleViolation`, to
refuse.

## Fields worked out before a commit: `before_save()`

`before_save()` is Django's `pre_save`: it runs on every aggregate a unit
of work saw, before each commit, just before `clean()`. Set there what
follows from the other fields, so no use case has to remember it:

```python
class Article(AggregateRoot[ArticleId]):
    title: Annotated[str, MaxLength(200)]
    slug: Annotated[str, Slug(), MaxLength(200)] = ""

    def before_save(self) -> None:
        self.slug = "-".join(self.title.lower().split())
```

What it sets is checked as any field is, and `clean()` sees the result.
It should not record events: they say what a use case asked for. A
reaction to the stored change is an event handler, after the commit.

## What a refusal looks like

`InvalidField` is a `BusinessRuleViolation`: the API answers 422, code
`invalid_field`, with the field named:

```json
{
  "detail": "stock: Use 0 or more.",
  "code": "invalid_field",
  "errors": [{"loc": ["body", "stock"], "msg": "stock: Use 0 or more.", "type": "invalid_field"}]
}
```

## What it costs

A class with no markers keeps Python's own attribute setting and its
dataclass `__init__`. One with markers checks only its marked fields, with
code made for that class on first use: about 0.4 µs per checked change and
1 µs per object made, and no added call per request on the benchmark
ladder. Loading from the database sets fields directly and checks nothing:
what is stored was checked when it was written.
