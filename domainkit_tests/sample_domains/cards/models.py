"""Card holders, whose card numbers are sealed under their own keys, and payments."""

from typing import Annotated
from uuid import UUID

from domainkit.ddd import AggregateRoot, Encrypted, Personal, ValueObject


class CardNumber(ValueObject):
    """A primary account number (PAN), as text."""

    value: str


class Cardholder(AggregateRoot[UUID]):
    """A person with a card: the subject of their own data."""

    name: Annotated[str, Personal(of="id")]
    card_number: Annotated[str | None, Encrypted(subject="id", blind_index=True)] = None
    passport: Annotated[str | None, Personal(of="id"), Encrypted()] = None
    """Sealed for the holder too: its ``Personal`` marker names them."""
    api_secret: Annotated[bytes | None, Encrypted()] = None
    """Sealed under the data key: no person's."""


class Payment(AggregateRoot[UUID]):
    """A payment by card, whose number is looked up by its blind index."""

    holder_id: UUID
    pan: Annotated[CardNumber, Encrypted(blind_index=True)]
    note: Annotated[str | None, Encrypted(subject="holder_id")] = None
    """Sealed for whoever holds the card: a new holder seals it again."""
