"""Aggregates with personal data privacy cannot reach or erase."""

from typing import Annotated
from uuid import UUID

from domainkit.ddd import AggregateRoot, Personal, ValueObject


class Note(AggregateRoot[UUID]):
    """Bytes cannot be erased by default."""

    author_id: UUID
    text: Annotated[str, Personal(of="author_id")]
    scan: Annotated[bytes, Personal(of="author_id")] = b""


class Memo(AggregateRoot[UUID]):
    """A personal field without a column cannot be read or erased."""

    author_id: UUID
    nickname: Annotated[str | None, Personal(of="author_id")] = None


class Ticket(AggregateRoot[int]):
    """Its unique code is too short to hold a unique erased value."""

    owner: str
    code: Annotated[str, Personal(of="owner")]


class Line(ValueObject):
    """Personal data deep inside a field."""

    name: Annotated[str, Personal(of="name")]
    quantity: int


class Basket(AggregateRoot[UUID]):
    """The marker is on a value object, not on the field."""

    lines: list[Line]


class Secret(AggregateRoot[str]):
    """Its personal field is its key."""

    id: Annotated[str, Personal(of="owner")]
    owner: str
