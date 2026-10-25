"""Pages over HTTP: opaque cursors, page sizes, and offsets.

``Pagination`` is a FastAPI dependency that reads ``limit`` and ``cursor``
(or ``offset``) from the query string; the ``PageRequest`` it gives fetches
the page from a repository and answers with a ``PageOut``::

    pages = Pagination(max_limit=100)

    @router.get("/orders")
    async def list_orders(
        page: Annotated[PageRequest, Depends(pages)], uow: UnitOfWorkDep
    ) -> PageOut[OrderOut]:
        query = Query(ordering=["-placed_at"])
        async with uow:
            found = await page.fetch(uow.orders, query)
        return page.respond(found, [OrderOut.of(o) for o in found.items], query)

A cursor holds the position of the last item a client received: the values
of the ordering's fields, the id last, as versioned base64url JSON with the
ordering it belongs to (ADR 0023). It is opaque, not secret. Decoding checks
everything (the version, the ordering, how many values there are, and each
value's type against its field), so a tampered cursor is a 422
``invalid_cursor``, never a 500, and its values only ever reach SQL as bound
parameters.

Cursors that come in are at most ``MAX_CURSOR_LENGTH`` long. Cursors that go
out are never refused, so a page always answers: ``cursor_length`` bounds
them by the ordering's fields (``MaxLength`` on text, ``Digits`` on
amounts), and ``FilterSet`` refuses, when declared, an ordering whose
cursor could be longer than ``MAX_CURSOR_LENGTH``.

``encode_cursor`` and ``decode_cursor`` are the older, untyped cursors of
plain strings, for hand-written keyset paging.
"""

import base64
import json
import logging
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from inspect import Parameter, Signature
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import Query as QueryParameter
from fastapi import status
from pydantic import BaseModel

from domainkit.ddd.exceptions import DomainError
from domainkit.ddd.fields import (
    Digits,
    FieldInfo,
    MaxLength,
    base_field,
    base_type,
    fields_of,
    primitive,
)
from domainkit.ddd.model import AggregateRoot
from domainkit.ddd.pagination import Page
from domainkit.ddd.query import (
    ID,
    CriteriaLike,
    Ordering,
    Query,
    as_query,
    total_ordering,
)
from domainkit.ddd.repository import Repository
from domainkit.http.errors import APIError
from domainkit.utils.decimals import decimal_text

CURSOR_PATTERN: Final = r"^[A-Za-z0-9_-]+$"
"""Cursors are base64url; use as ``Query(pattern=CURSOR_PATTERN)``."""
MAX_CURSOR_LENGTH: Final = 4096
"""Longest cursor accepted; use as ``Query(max_length=MAX_CURSOR_LENGTH)``.

Enough for 3,072 bytes of JSON: an ordering by three text fields of
``MaxLength(150)``, in any script, with a UUID id (see ``cursor_length``)."""
CURSOR_VERSION: Final = 1
"""The format of cursors ``encode_position`` makes (ADR 0023)."""
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200
MAX_OFFSET: Final = 10_000
"""The deepest offset ``Pagination(offset=True)`` accepts by default."""
_INT64: Final = 2**63
_AMOUNT_DIGITS: Final = 40
"""Digits a cursor's amount may have before the point, and after it."""
_DECIMAL: Final = re.compile(
    rf"-?\d{{1,{_AMOUNT_DIGITS}}}(\.\d{{1,{_AMOUNT_DIGITS}}})?"
)
_ESCAPED: Final = 6
"""The most bytes one character of text takes in a cursor: JSON writes a
control character as ``\\u001f``, and UTF-8 any other in at most 4."""
_SIZES: Final[dict[type | None, int]] = {
    bool: len("false"),
    int: len(str(-_INT64)),
    float: len("-2.2250738585072014e-308"),
    UUID: len('"00000000-0000-0000-0000-000000000000"'),
    datetime: len('"0001-01-01T00:00:00.000001+05:30:15.000001"'),
    date: len('"0001-01-01"'),
    time: len('"00:00:00.000001+05:30:15.000001"'),
}
"""The most bytes a value of each type takes in a cursor, as JSON."""
_NULL: Final = len("null")

logger = logging.getLogger(__name__)


class InvalidCursor(APIError):
    """The cursor was not issued by this API, or was altered."""

    def __init__(self) -> None:
        """Answer with 422 and the ``invalid_cursor`` code."""
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The cursor is invalid; start again without one.",
            code="invalid_cursor",
        )


def encode_cursor(values: Sequence[str]) -> str:
    """Encode a sort key as an opaque, URL-safe cursor.

    Raises:
        ValueError: The cursor would be longer than ``decode_cursor`` accepts
            (sort keys must stay short).
    """
    cursor = _encode(list(values))
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError(f"Sort key too long for a cursor ({len(cursor)} chars).")
    return cursor


def decode_cursor(cursor: str, size: int) -> list[str]:
    """Decode a cursor made by ``encode_cursor`` with ``size`` values.

    Raises:
        InvalidCursor: The cursor is malformed or has the wrong shape.
    """
    values = _decode(cursor)
    if (
        not isinstance(values, list)
        or len(values) != size
        or not all(isinstance(value, str) for value in values)
    ):
        raise InvalidCursor
    return values


def encode_position(position: Sequence[object], ordering: Sequence[Ordering]) -> str:
    """An opaque cursor for ``position`` in ``ordering`` (a page's ``next_position``).

    It is never refused for its length, so the page it ends always answers.
    Its fields' limits bound it (``cursor_length``). A cursor longer than
    ``MAX_CURSOR_LENGTH`` means a value is longer than its field declares,
    or an ordering no ``FilterSet`` checked: it is logged, and refused when
    it comes back.

    Raises:
        TypeError: A value has no cursor form.
    """
    values = [_to_json(value) for value in position]
    cursor = _encode([CURSOR_VERSION, _signature(ordering), *values])
    if len(cursor) > MAX_CURSOR_LENGTH:
        logger.warning(
            "A cursor for the ordering %s took %d characters, more than "
            "MAX_CURSOR_LENGTH: a value is longer than its field declares.",
            _signature(ordering),
            len(cursor),
        )
    return cursor


def decode_position(
    cursor: str, ordering: Sequence[Ordering], model: type
) -> tuple[object, ...]:
    """The position a cursor from ``encode_position`` holds, checked field by field.

    Raises:
        InvalidCursor: The cursor is malformed, of another version or
            ordering, or holds a value that does not fit its field.
    """
    payload = _decode(cursor)
    if (
        not isinstance(payload, list)
        or len(payload) != len(ordering) + 2
        or type(payload[0]) is not int
        or payload[0] != CURSOR_VERSION
        or payload[1] != _signature(ordering)
    ):
        raise InvalidCursor
    fields = fields_of(model)
    return tuple(
        _from_json(fields[item.bind(model).field], value)
        for item, value in zip(ordering, payload[2:], strict=True)
    )


def cursor_length(ordering: Sequence[Ordering], model: type) -> int:
    r"""The longest cursor ``encode_position`` makes for ``ordering`` over ``model``.

    The ordering is completed with the id, as pages complete it. The bound
    follows from each field's type and declared limits: text needs
    ``MaxLength`` (each character may take 6 bytes, as ``\u001f``),
    amounts ``Digits``; enums take their longest value. Values longer than
    their field declares make longer cursors.

    Raises:
        TypeError: A field has no bound in a cursor: text without
            ``MaxLength``, an amount without ``Digits``, or a type cursors
            cannot hold. The message says what to declare.
        InvalidQuery: ``model`` has no such field, or it does not sort.
    """
    complete = total_ordering(ordering)
    head = len(_json([CURSOR_VERSION, _signature(complete)]))
    values = sum(1 + _value_size(_field(model, item.field)) for item in complete)
    return (4 * (head + values) + 2) // 3  # Base64 without padding.


def longest_ordering(
    fields: Iterable[str], model: type, count: int
) -> tuple[Ordering, ...]:
    """Of the orderings by at most ``count`` of ``fields``, one with the longest cursor.

    A client that may order by up to ``count`` of ``fields``, each either
    way (``FilterSet``'s ``ordering_fields`` and ``max_ordering``), makes
    no cursor longer than ``cursor_length`` of this one: the fields whose
    names and values take most room, descending.

    Raises:
        TypeError: A field has no bound in a cursor (see ``cursor_length``).
        InvalidQuery: ``model`` has no such field, or it does not sort.
    """
    names = list(dict.fromkeys(fields))
    room = {
        name: len(name.encode()) + _value_size(_field(model, name))
        for name in names
        if name != ID
    }
    chosen = sorted(room, key=room.__getitem__, reverse=True)[:count]
    ordering = [Ordering(name, descending=True) for name in chosen]
    if ID in names and len(ordering) < count:  # "-id" is a byte longer than "id".
        ordering.append(Ordering(ID, descending=True))
    return tuple(ordering)


def _field(model: type, name: str) -> FieldInfo:
    """The field ``name`` of ``model``, which must sort.

    Raises:
        InvalidQuery: ``model`` has no such field, or it does not sort.
    """
    return fields_of(model)[Ordering(name).bind(model).field]


def _value_size(field: FieldInfo) -> int:
    """The most bytes a value of ``field`` takes in a cursor, as JSON.

    Raises:
        TypeError: The field's values have no bound, or no cursor form.
    """
    kind = base_type(field.kind)
    where = f"{field.owner.__name__}.{field.name}"
    if kind is not None and issubclass(kind, Enum):
        size = max((len(_json(_to_json(member))) for member in kind), default=0)
    elif kind is str:
        limit = base_field(field).marker(MaxLength)
        if limit is None:
            raise TypeError(
                f"{where} holds text of any length, which no cursor can bound: "
                "declare its MaxLength(n), as Annotated[str, MaxLength(n)], or "
                "do not order by it."
            )
        size = 2 + _ESCAPED * limit.length
    elif kind is Decimal:
        digits = base_field(field).marker(Digits)
        if digits is None:
            raise TypeError(
                f"{where} holds amounts of any size, which no cursor can bound: "
                "declare its Digits(precision, scale), as Annotated[Decimal, "
                "Digits(precision, scale)], or do not order by it."
            )
        whole = max(digits.precision - digits.scale, 1)
        point = digits.scale + 1 if digits.scale else 0
        size = 3 + whole + point  # The quotes and a sign.
    elif (known := _SIZES.get(kind)) is not None:
        size = known
    else:
        held = kind.__name__ if kind is not None else "values of no known type"
        raise TypeError(
            f"{where} holds {held}, which a cursor cannot hold: do not order by it."
        )
    return max(size, _NULL) if field.nullable else size


def _signature(ordering: Sequence[Ordering]) -> str:
    """The ordering as text, ``-placed_at,id``: a cursor belongs to one."""
    return ",".join(map(str, ordering))


def _json(payload: object) -> bytes:
    """``payload`` as compact UTF-8 JSON, as cursors hold it."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def _encode(payload: object) -> str:
    """Base64url JSON, without padding."""
    return base64.urlsafe_b64encode(_json(payload)).rstrip(b"=").decode()


def _decode(cursor: str) -> object:
    """The JSON inside a cursor.

    Raises:
        InvalidCursor: Too long, not base64url, or not JSON.
    """
    if len(cursor) > MAX_CURSOR_LENGTH or not re.fullmatch(CURSOR_PATTERN, cursor):
        raise InvalidCursor
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except ValueError as exc:  # binascii and Unicode errors are ValueErrors.
        raise InvalidCursor from exc


def _to_json(value: object) -> object:
    """A position value as JSON: amounts, times, and ids as text."""
    value = primitive(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Decimal):  # Plain: never 0E-8, which decoding refuses.
        exponent = value.as_tuple().exponent  # Of the last digit; a letter if NaN.
        plain = (
            isinstance(exponent, int)
            and exponent >= -_AMOUNT_DIGITS
            and value.adjusted() < _AMOUNT_DIGITS
        )
        return decimal_text(value) if plain else str(value)  # Not 1E+999999999.
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date | time):  # A datetime is a date.
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} values cannot be put in a cursor.")


def _from_json(field: FieldInfo, value: object) -> object:
    """``value`` from a cursor, as ``field`` holds it.

    Raises:
        InvalidCursor: It is not what the field holds.
    """
    if value is None:
        if not field.nullable:
            raise InvalidCursor
        return None
    kind = base_type(field.kind)
    if kind is not None and issubclass(kind, Enum):
        members = [member.value for member in kind]
        kind = type(members[0]) if members else None
    try:
        parsed = _parse(kind, value)
        return field.coerce(parsed)
    except (ValueError, TypeError, DomainError) as exc:
        raise InvalidCursor from exc


def _parse(kind: type | None, value: object) -> object:
    """Read ``value``, as JSON gave it, as a value of ``kind``.

    Raises:
        ValueError: It is not one.
    """
    if kind is bool:
        return _of(bool, value)
    if kind is int:
        number = _of(int, value)
        if isinstance(number, bool) or abs(number) >= _INT64:
            raise ValueError("not a 64-bit integer")
        return number
    if kind is str:
        return _of(str, value)
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("not a number")
        return float(value)
    if kind is Decimal:
        text = _of(str, value)
        if not _DECIMAL.fullmatch(text):
            raise ValueError("not an amount")
        return Decimal(text)
    if kind is UUID:
        return UUID(_of(str, value))
    if kind is datetime:
        return datetime.fromisoformat(_of(str, value))
    if kind is date:
        return date.fromisoformat(_of(str, value))
    if kind is time:
        return time.fromisoformat(_of(str, value))
    raise ValueError("this field cannot be in a cursor")


def _of[T](kind: type[T], value: object) -> T:
    """``value`` if it is a ``kind``; bools are not integers here.

    Raises:
        ValueError: It is not.
    """
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ValueError(f"not a {kind.__name__}")
    return value


class PageOut[ItemT](BaseModel):
    """A page of items: pass ``next_cursor`` back as ``cursor`` for the next.

    In offset mode ``next_offset`` is set instead. ``total`` counts every
    match when the endpoint counts.
    """

    items: list[ItemT]
    next_cursor: str | None = None
    next_offset: int | None = None
    total: int | None = None


@dataclass(frozen=True, slots=True)
class PageRequest:
    """The page a request asks for; ``Pagination`` makes it from the query string."""

    limit: int
    cursor: str | None = None
    offset: int | None = None
    """Set in offset mode only, where it replaces the cursor."""
    count: bool = False

    async def fetch[AggregateT: AggregateRoot[Any]](
        self,
        repository: Repository[AggregateT, Any],
        query: Query | CriteriaLike | None = None,
    ) -> Page[AggregateT]:
        """Fetch the page of ``query`` (its ordering, then the id) from ``repository``.

        Raises:
            InvalidCursor: The cursor does not fit the query's ordering or
                the aggregate's fields.
        """
        ordering = total_ordering(as_query(query).ordering)
        after = None
        if self.cursor is not None:
            after = decode_position(self.cursor, ordering, repository.aggregate_type)
        return await repository.paginate(
            query,
            limit=self.limit,
            after=after,
            offset=self.offset or 0,
            count=self.count,
        )

    def respond[ItemT](
        self,
        page: Page[Any],
        items: list[ItemT],
        query: Query | CriteriaLike | None = None,
    ) -> PageOut[ItemT]:
        """The response for ``page``, whose items are shown as ``items``."""
        if self.offset is not None:
            following = self.offset + self.limit if page.has_more else None
            return PageOut(items=items, next_offset=following, total=page.total)
        cursor = None
        if page.next_position is not None:
            ordering = total_ordering(as_query(query).ordering)
            cursor = encode_position(page.next_position, ordering)
        return PageOut(items=items, next_cursor=cursor, total=page.total)


class Pagination:
    """A FastAPI dependency that reads a ``PageRequest`` from the query string.

    ``limit`` defaults to ``default_limit`` and may not exceed ``max_limit``.
    Pages continue with ``cursor``, or, with ``offset=True``, with
    ``offset``, up to ``max_offset``. ``count=True`` counts every match into
    ``total`` (one more query per page). OpenAPI documents the parameters
    with their bounds; values out of bounds are a 422 ``validation_error``.
    """

    def __init__(
        self,
        *,
        default_limit: int = DEFAULT_LIMIT,
        max_limit: int = MAX_LIMIT,
        offset: bool = False,
        max_offset: int = MAX_OFFSET,
        count: bool = False,
    ) -> None:
        """Describe the parameters; they are checked by FastAPI."""
        if not 1 <= default_limit <= max_limit:
            raise ValueError("Pagination needs 1 <= default_limit <= max_limit.")
        self.offset = offset
        self.count = count
        limit = Parameter(
            "limit",
            Parameter.KEYWORD_ONLY,
            default=default_limit,
            annotation=Annotated[
                int,
                QueryParameter(ge=1, le=max_limit, description="Items per page."),
            ],
        )
        if offset:
            position = Parameter(
                "offset",
                Parameter.KEYWORD_ONLY,
                default=0,
                annotation=Annotated[
                    int,
                    QueryParameter(ge=0, le=max_offset, description="Items to skip."),
                ],
            )
        else:
            position = Parameter(
                "cursor",
                Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Annotated[
                    str | None,
                    QueryParameter(
                        max_length=MAX_CURSOR_LENGTH,
                        pattern=CURSOR_PATTERN,
                        description="The previous page's next_cursor.",
                    ),
                ],
            )
        self.__signature__ = Signature([limit, position], return_annotation=PageRequest)

    async def __call__(
        self, *, limit: int, cursor: str | None = None, offset: int = 0
    ) -> PageRequest:
        """The page the query string asks for."""
        return self.page(limit, cursor, offset)

    def page(
        self, limit: int = DEFAULT_LIMIT, cursor: str | None = None, offset: int = 0
    ) -> PageRequest:
        """The ``PageRequest`` this dependency makes, for code and tests."""
        return PageRequest(
            limit=limit,
            cursor=None if self.offset else cursor,
            offset=offset if self.offset else None,
            count=self.count,
        )

    def __repr__(self) -> str:
        """``Pagination(offset=True, count=False)``."""
        return f"Pagination(offset={self.offset}, count={self.count})"
