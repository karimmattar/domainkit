# Validation

Validation keeps bad values out of your service. In domainkit you write
a field's limits once, on the field itself. The same limits then apply
everywhere: when your code changes the object, when a client sends a
request body, and when the database makes the column.

On this page you learn how to:

- limit text and numbers with markers such as `MaxLength` and `Range`;
- use the ready field types `Email`, `URL`, `Slug`, and `IPAddress`;
- check rules that involve more than one field, with `clean()`;
- fill in fields just before a commit, with `before_save()`;
- read the answer a client gets when a value is refused.

## A first example

A product has an SKU (a stock code), a name, and a stock count. Each field
says what it allows:

```python
from typing import Annotated, NewType
from uuid import UUID, uuid4

from domainkit.ddd import AggregateRoot, MaxLength, MinLength, Pattern, Range

ProductId = NewType("ProductId", UUID)
Sku = Annotated[str, MinLength(3), MaxLength(12), Pattern("[A-Z0-9-]+")]


class Product(AggregateRoot[ProductId]):
    sku: Sku
    name: Annotated[str, MaxLength(100)]
    stock: Annotated[int, Range(min=0)] = 0


lamp = Product(id=ProductId(uuid4()), sku="AB-12", name="Lamp")
lamp.stock = -1
```

The last line raises `InvalidField`:

```text
domainkit.ddd.exceptions.InvalidField: stock: Use 0 or more.
```

The message names the field, then says what to use instead. The object
keeps its old value, 0.

## Markers

A marker is a limit that you put inside `Annotated[...]`. You can put
several markers on one field.

| Marker | What it allows |
| --- | --- |
| `MaxLength(n)` | Text of at most `n` characters. |
| `MinLength(n)` | Text of at least `n` characters. |
| `Pattern(regex)` | Text that the regular expression matches from the first character to the last. |
| `Range(min=..., max=...)` | A number from `min` to `max`. Both ends are allowed. You may leave out either end. |
| `IPAddressFormat(version=None)` | An IPv4 or IPv6 address. Use `version=4` or `version=6` to allow only one of them. |

!!! tip
    In a `Pattern`, write `[0-9]` for a digit, not `\d`. In Python, `\d`
    also matches digits of other scripts, such as Arabic-Indic digits.

To reuse a set of markers, give it a name, as `Sku` does above. Every
field of that type then has the same limits.

## Ready field types

Four field types are ready to use. Each one is an `Annotated[str, ...]`
of the markers above, so it is checked and sized like any other field.

| Type | What it allows |
| --- | --- |
| `Email` | An email address: at most 254 characters, a local part, `@`, and a domain of two or more labels. |
| `URL` | An `http` or `https` address of at most 2,048 characters. |
| `Slug` | Latin letters, digits, `-`, and `_`, at most 50 characters. |
| `IPAddress` | An IPv4 or IPv6 address. |

```python
from typing import Annotated, NewType
from uuid import UUID

from domainkit.ddd import URL, AggregateRoot, Email, Slug, Unique

SiteId = NewType("SiteId", UUID)


class Site(AggregateRoot[SiteId]):
    contact: Email
    slug: Annotated[Slug, Unique()]
    home: URL | None = None
```

`Site(..., contact="not-an-email", slug="shop")` raises `InvalidField`
with the message `contact: Enter a value in the expected format.`

!!! note
    An address that `Email` allows may still not receive mail. Only
    sending a message to it shows that.

## Where they are checked

The same limits are checked in three places:

- **The object.** An entity or an aggregate checks a field each time the
  field is set: when the object is made, and on every later change. Value
  objects, commands, and events check their fields once, when they are
  made. The parameters of a `@use_case` are the fields of its command, so
  `await restock(uow, quantity=0)` is refused before the function runs.
- **The request.** `schema_for` and `ResourceRouter.use_case` build the
  request bodies with the same limits. A bad body gets a 422 answer
  before any use case runs. The OpenAPI document shows the limits too.
- **The database.** `table_for` sizes each text column from its
  `MaxLength`.

`None` is never checked. A field of type
`Annotated[str, MaxLength(3)] | None` allows `None`.

## Rules across fields: `clean()`

Some rules involve more than one field. For example, a discontinued
product must have no stock. Write such a rule in `clean()`, and raise a
`DomainError` subclass, such as `BusinessRuleViolation`, to refuse:

```python
from domainkit.ddd import BusinessRuleViolation


class Product(AggregateRoot[ProductId]):
    sku: Sku
    stock: Annotated[int, Range(min=0)] = 0
    discontinued: bool = False

    def clean(self) -> None:
        if self.discontinued and self.stock:
            raise BusinessRuleViolation("A discontinued product holds no stock.")
```

`clean()` runs at two moments:

1. when the object is made;
2. just before a unit of work commits, on every aggregate that the unit
   of work saw.

Between these moments, a use case may change fields one at a time. The
object may break the rule for a short time. Only what is committed must
keep the rule.

Django calls this hook `clean()` too.

## Fields worked out before a commit: `before_save()`

Some fields follow from other fields. A slug, for example, follows from a
title. Set such fields in `before_save()`. Then no use case has to
remember to set them:

```python
from typing import Annotated, NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot, MaxLength, Slug

ArticleId = NewType("ArticleId", UUID)


class Article(AggregateRoot[ArticleId]):
    title: Annotated[str, MaxLength(200)]
    slug: Slug | None = None

    def before_save(self) -> None:
        self.slug = "-".join(self.title.lower().split())
```

`before_save()` runs before each commit, on every aggregate that the unit
of work saw, just before `clean()`. The values it sets are checked like
any other value, and `clean()` sees them.

Do not record events in `before_save()`. An event says what a use case
asked for. To react to a stored change, write an event handler: it runs
after the commit.

Django calls this hook `pre_save`.

## What a refusal looks like

`InvalidField` is a kind of `BusinessRuleViolation`. When it reaches the
API, the client gets a 422 answer with the code `invalid_field`, and the
field is named in `errors`:

```json
{
  "detail": "stock: Use 0 or more.",
  "code": "invalid_field",
  "errors": [
    {"loc": ["body", "stock"], "msg": "stock: Use 0 or more.", "type": "invalid_field"}
  ]
}
```

A `BusinessRuleViolation` that you raise yourself, as in `clean()`, also
gets a 422 answer. [Problems](reference/problems.md) lists every code.

## What it costs

A class with no markers sets its attributes as plain Python does, and
uses the plain dataclass `__init__`. A class with markers checks only its
marked fields. It builds the checking code for the class the first time
the class is used. A checked change costs about 0.4 µs, and making an
object costs about 1 µs. On the benchmark ladder, validation adds no
call per request.

Loading an object from the database checks nothing. What is stored was
checked when it was written.

## Common mistakes

- **Writing `\d` in a `Pattern`.** It also matches digits of other
  scripts. Write `[0-9]`.
- **Putting a cross-field rule in a setter.** A use case may change two
  fields one after the other, and the first change then fails. Put the
  rule in `clean()`, which runs on what is committed.
- **Expecting `None` to be checked.** `None` always passes. Leave out
  `| None` if the field must have a value.
- **Recording an event in `before_save()`.** Write an event handler
  instead.

## See also

- [Resources](resources.md): request bodies made from your fields
  (`schema_for`).
- [Databases](databases.md#tables-from-domain-classes): columns made from
  your fields (`table_for`).
- [Problems](reference/problems.md): every error code a client can get.
- [The glossary](learning.md#words): aggregate, entity, value object,
  unit of work.
