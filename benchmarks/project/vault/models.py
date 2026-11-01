"""A wallet holding money, and a card whose number is encrypted."""

from typing import Annotated, NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot, Encrypted
from domainkit.money import Money

WalletId = NewType("WalletId", UUID)
CardId = NewType("CardId", UUID)


class Wallet(AggregateRoot[WalletId]):
    """Someone's money: an amount and its currency, two columns."""

    owner: str
    balance: Money


class Card(AggregateRoot[CardId]):
    """A card, its number sealed under the current data key."""

    holder: str
    number: Annotated[str, Encrypted()]
