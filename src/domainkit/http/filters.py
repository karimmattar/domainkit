"""Filters, ordering, and search from the query string (django-filter's FilterSet).

A ``FilterSet`` names, per field, the lookups clients may use, and turns
the query string into a ``Query``::

    class OrderFilters(FilterSet):
        model = Order
        status = Filter("exact", "in")
        total = Filter("gte", "lte")
        customer = Filter("icontains", field="customer_name")
        ordering_fields = ("placed_at", "total")
        default_ordering = ("-placed_at",)
        search_fields = ("customer_name", "reference")

    @router.get("/orders")
    async def list_orders(filters: Annotated[OrderFilters, Depends()], ...):
        query = filters.query()

``?status=open&total__gte=10&ordering=-placed_at&search=acme`` then means
``status == open``, ``total >= 10``, newest first, and every word of the
search in one of the search fields. ``__in`` and ``ordering`` repeat:
``?status__in=open&status__in=paid``.

Parameters are typed from the model's fields, so OpenAPI lists each with
its type (and an enum's values), and FastAPI answers a value of the wrong
type with 422 ``validation_error``. What FastAPI cannot see (an unknown
lookup such as ``total__regex``, a filter given twice, a repeated ordering
field, or more conditions, values, or search words than the bounds allow)
is refused with 422 ``invalid_filter``. Values only ever reach SQL as bound
parameters.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum
from inspect import Parameter, Signature
from typing import Annotated, Any, ClassVar, Final, Literal

from fastapi import Query as QueryParameter
from fastapi import Request, status
from pydantic import AwareDatetime, Field

from domainkit.ddd.exceptions import DomainError
from domainkit.ddd.fields import FieldInfo, InvalidQuery, base_type, fields_of
from domainkit.ddd.query import (
    ORDERING_LOOKUPS,
    TEXT_LOOKUPS,
    And,
    Criteria,
    F,
    Lookup,
    Or,
    Ordering,
    Query,
    resolve,
)
from domainkit.http.errors import APIError

SEPARATOR: Final = "__"
"""Between a field and its lookup: ``total__gte``."""
ORDERING: Final = "ordering"
SEARCH: Final = "search"
REQUEST: Final = "_request"
_INT64: Final = 2**63
_DESCRIPTIONS: Final = {
    Lookup.EXACT: "is this",
    Lookup.IEXACT: "is this, ignoring case",
    Lookup.CONTAINS: "contains this",
    Lookup.ICONTAINS: "contains this, ignoring case",
    Lookup.STARTSWITH: "starts with this",
    Lookup.ISTARTSWITH: "starts with this, ignoring case",
    Lookup.ENDSWITH: "ends with this",
    Lookup.IENDSWITH: "ends with this, ignoring case",
    Lookup.GT: "is greater than this",
    Lookup.GTE: "is at least this",
    Lookup.LT: "is less than this",
    Lookup.LTE: "is at most this",
    Lookup.IN: "is one of these",
    Lookup.ISNULL: "is missing (true) or present (false)",
}


class InvalidFilter(APIError):
    """The query string asks for a filter the endpoint does not offer."""

    def __init__(self, detail: str) -> None:
        """Answer with 422 and the ``invalid_filter`` code."""
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail, code="invalid_filter"
        )


class Filter:
    """One filtered field of a ``FilterSet`` and the lookups it allows.

    The field defaults to the attribute's name; ``field`` names another, or
    a child entity's (``"lines.sku"``). ``exact`` is the bare parameter
    (``?status=open``); every other lookup is ``<name>__<lookup>``.
    ``range`` is not offered: use ``gte`` and ``lte``.
    """

    def __init__(
        self, *lookups: Lookup | str, field: str | None = None, max_length: int = 200
    ) -> None:
        """Allow ``lookups`` (default: ``exact``) on ``field``.

        ``max_length`` bounds text values.
        """
        self.lookups = tuple(
            dict.fromkeys(Lookup(each) for each in lookups or ("exact",))
        )
        if Lookup.RANGE in self.lookups:
            raise ValueError("Offer gte and lte instead of range.")
        self.field = field
        self.max_length = max_length
        self.name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        """Take the attribute's name, and the field's if none was given."""
        self.name = name
        self.field = self.field or name

    def parameter(self, lookup: Lookup) -> str:
        """The query parameter of ``lookup``: ``total`` or ``total__gte``."""
        return (
            self.name if lookup is Lookup.EXACT else f"{self.name}{SEPARATOR}{lookup}"
        )


class FilterSet:
    """Declarative filters, ordering, and search for one aggregate's listing.

    Subclasses set ``model`` and declare ``Filter`` attributes, and may set
    the class variables below. Used as a FastAPI dependency
    (``Annotated[MyFilters, Depends()]``), the class reads its parameters
    from the query string; ``query()`` and ``criteria`` give the result.
    """

    model: ClassVar[type]
    ordering_fields: ClassVar[tuple[str, ...]] = ()
    """Fields clients may order by, each ascending or descending."""
    default_ordering: ClassVar[tuple[str, ...]] = ()
    """The ordering without ``?ordering=`` (``"-placed_at"``); the id ends every one."""
    search_fields: ClassVar[tuple[str, ...]] = ()
    """Text fields ``?search=`` looks in (case-insensitively)."""
    max_conditions: ClassVar[int] = 10
    """The most filters and search words one request may combine."""
    max_values: ClassVar[int] = 50
    """The most values an ``__in`` filter may list."""
    max_search_length: ClassVar[int] = 100
    max_search_words: ClassVar[int] = 5
    max_ordering: ClassVar[int] = 3
    """The most fields one ``?ordering=`` may name."""

    filters: ClassVar[Mapping[str, tuple[Filter, Lookup, FieldInfo]]] = {}
    """Every query parameter, with its filter, lookup, and field."""

    def __init_subclass__(cls) -> None:
        """Check the declarations against the model and build the parameters."""
        super().__init_subclass__()
        if not hasattr(cls, "model"):
            return
        declared = [
            value
            for klass in reversed(cls.__mro__)
            for value in vars(klass).values()
            if isinstance(value, Filter)
        ]
        cls.filters = {
            each.parameter(lookup): (each, lookup, _checked(cls.model, each, lookup))
            for each in declared
            for lookup in each.lookups
        }
        if taken := {ORDERING, SEARCH, REQUEST} & set(cls.filters):
            raise TypeError(f"{cls.__name__}: {', '.join(taken)} is not a filter name.")
        for name in (*cls.ordering_fields, *_fields(cls.default_ordering)):
            _orderable(cls.model, name)
        for name in cls.search_fields:
            if not resolve(cls.model, name)[-1].text:
                raise TypeError(f"{cls.__name__}: {name} is not text to search.")
        setattr(cls, "__signature__", Signature(_parameters(cls)))  # noqa: B010 - not a declared attribute.

    def __init__(self, _request: Request | None = None, **values: Any) -> None:  # noqa: ANN401 - each parameter's type.
        """Take the parameters FastAPI parsed; refuse what it cannot check.

        Raises:
            InvalidFilter: An unknown or repeated filter, a repeated ordering
                field, or more conditions or search words than allowed.
        """
        if _request is not None:
            self._check_names(_request)
        given = {name: value for name, value in values.items() if value is not None}
        ordering: list[str] = list(given.pop(ORDERING, ()))
        search: str = given.pop(SEARCH, "")
        words = search.split()
        if len(words) > self.max_search_words:
            raise InvalidFilter(f"Search for at most {self.max_search_words} words.")
        if len(given) + len(words) > self.max_conditions:
            raise InvalidFilter(f"Combine at most {self.max_conditions} filters.")
        if len({item.removeprefix("-") for item in ordering}) != len(ordering):
            raise InvalidFilter("Name each field once in the ordering.")
        unknown = set(given) - set(self.filters)
        if unknown:
            raise InvalidFilter(f"Unknown filter: {', '.join(sorted(unknown))}.")
        self.values = given
        self.search = words
        self.ordering = tuple(
            Ordering.parse(item) for item in ordering or self.default_ordering
        )
        self.criteria = self._criteria()

    def query(self, **options: Any) -> Query:  # noqa: ANN401 - Query's keywords.
        """The query: the criteria and the ordering, plus ``options`` (``limit=``)."""
        return Query(self.criteria, ordering=self.ordering, **options)

    def _criteria(self) -> Criteria | None:
        """The conditions the parameters and search words ask for.

        Raises:
            InvalidFilter: A value does not fit its field (a value object
                refused it, say).
        """
        parts: list[Criteria] = []
        for name, value in self.values.items():
            each, lookup, _ = self.filters[name]
            argument = tuple(value) if lookup is Lookup.IN else value
            parts.append(F(each.field or name).lookup(lookup, argument))
        parts.extend(
            Or.of(*(F(name).icontains(word) for name in self.search_fields))
            for word in self.search
        )
        if not parts:
            return None
        criteria = And.of(*parts)
        try:
            criteria.bind(self.model)
        except (InvalidQuery, ValueError, TypeError, DomainError) as exc:
            raise InvalidFilter(
                f"A filter value does not fit its field: {exc}"
            ) from exc
        return criteria

    def _check_names(self, request: Request) -> None:
        """Refuse lookups this set does not offer, and filters given twice.

        Messages name only the filters the set declares, never what the
        client sent.
        """
        offered: dict[str, list[str]] = {}
        for name in self.filters:
            offered.setdefault(name.partition(SEPARATOR)[0], []).append(name)
        for name in request.query_params:
            base, separator, _ = name.partition(SEPARATOR)
            if name in self.filters:
                if len(request.query_params.getlist(name)) > 1 and not name.endswith(
                    f"{SEPARATOR}{Lookup.IN}"
                ):
                    raise InvalidFilter(f"Give the {name} filter once.")
            elif separator and base in offered:
                raise InvalidFilter(
                    f"Unknown lookup on {base}; it offers {', '.join(offered[base])}."
                )
            elif separator and base in fields_of(self.model):
                raise InvalidFilter(f"The listing cannot be filtered on {base}.")


def _checked(model: type, each: Filter, lookup: Lookup) -> FieldInfo:
    """The field ``each`` filters, if ``lookup`` applies to it.

    Raises:
        TypeError: It does not.
    """
    field = resolve(model, each.field or each.name)[-1]
    if lookup in TEXT_LOOKUPS and not field.text:
        raise TypeError(f"{each.name}: {lookup} needs a text field.")
    if lookup in ORDERING_LOOKUPS and not field.orderable:
        raise TypeError(f"{each.name}: {lookup} needs a field whose values sort.")
    return field


def _orderable(model: type, name: str) -> None:
    """Refuse an ordering field that does not exist or does not sort."""
    Ordering(name).bind(model)


def _fields(ordering: Iterable[str]) -> list[str]:
    """The field names of ``"-placed_at"``-style orderings."""
    return [item.removeprefix("-") for item in ordering]


def _parameters(cls: type[FilterSet]) -> list[Parameter]:
    """FastAPI's view of the set: one typed query parameter per filter."""
    parameters = [
        Parameter(REQUEST, Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)
    ]
    for name, (each, lookup, field) in cls.filters.items():
        parameters.append(
            Parameter(
                name,
                Parameter.KEYWORD_ONLY,
                default=None,
                annotation=_annotation(cls, each, lookup, field),
            )
        )
    if cls.ordering_fields:
        choices = tuple(
            f"{sign}{name}" for name in cls.ordering_fields for sign in ("", "-")
        )
        parameters.append(
            Parameter(
                ORDERING,
                Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Annotated[
                    list[Literal[choices]] | None,  # type: ignore[valid-type]
                    QueryParameter(
                        max_length=cls.max_ordering,
                        description="Order by these fields; - for descending.",
                    ),
                ],
            )
        )
    if cls.search_fields:
        parameters.append(
            Parameter(
                SEARCH,
                Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Annotated[
                    str | None,
                    QueryParameter(
                        max_length=cls.max_search_length,
                        description=(
                            f"Words to find in {', '.join(cls.search_fields)}, "
                            "ignoring case."
                        ),
                    ),
                ],
            )
        )
    return parameters


def _annotation(
    cls: type[FilterSet], each: Filter, lookup: Lookup, field: FieldInfo
) -> object:
    """The typed, documented annotation of one filter parameter."""
    description = f"Only items whose {each.field} {_DESCRIPTIONS[lookup]}."
    if lookup is Lookup.ISNULL:
        return Annotated[bool | None, QueryParameter(description=description)]
    kind: Any = str if lookup in TEXT_LOOKUPS else _value_type(field)
    if kind is str:
        kind = Annotated[str, Field(max_length=each.max_length)]
    elif kind is int:  # What a 64-bit column holds.
        kind = Annotated[int, Field(ge=-_INT64, lt=_INT64)]
    if lookup is Lookup.IN:
        return Annotated[
            list[kind] | None,
            QueryParameter(max_length=cls.max_values, description=description),
        ]
    return Annotated[kind | None, QueryParameter(description=description)]


def _value_type(field: FieldInfo) -> object:
    """The type a parameter for ``field`` parses: an enum, or what a value wraps."""
    kind = field.kind
    if kind is not None and not issubclass(kind, Enum):
        kind = base_type(kind)
    if kind is None:
        return str
    if kind is datetime:
        return AwareDatetime
    return kind
