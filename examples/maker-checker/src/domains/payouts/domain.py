"""The business of the payouts domain: the ``Payout`` aggregate and its rules.

Plain Python: only ``domainkit.ddd``, ``domainkit.money``, and the standard
library. Who made a payout and who approved it is the audit trail's to
say: the events of each step name their actor.
"""

from abc import ABC
from typing import Annotated, Final, NewType, Self
from uuid import UUID, uuid4

from domainkit.ddd import (
    AggregateRoot,
    BusinessRuleViolation,
    Digits,
    DomainEvent,
    MaxLength,
    NotFound,
    Repository,
    UnitOfWork,
)
from domainkit.money import Money

PayoutId = NewType("PayoutId", UUID)
Payee = Annotated[str, MaxLength(100)]
LONGEST_IBAN: Final = 34
SHORTEST_IBAN: Final = 15
Iban = Annotated[str, MaxLength(LONGEST_IBAN)]
"""An IBAN as banks store it: no spaces, at most 34 characters."""
PrintedIban = Annotated[str, MaxLength(LONGEST_IBAN + LONGEST_IBAN // 4)]
"""An IBAN as people type it: in groups of four, a space between."""


class PayoutNotFound(NotFound):
    """No payout has the requested id."""


class InvalidPayout(BusinessRuleViolation):
    """A payout names its payee and their IBAN, and moves more than nothing."""


class PayoutSent(DomainEvent):
    """Money left for a payee's bank account."""

    payout_id: PayoutId
    payee: str
    amount: Money


class Payout(AggregateRoot[PayoutId]):
    """Money sent to a supplier's bank account."""

    payee: Payee
    iban: Iban
    amount: Annotated[Money, Digits(14, 2)]

    class Meta:
        """Permissions, as Django's ``Meta``: approving large payouts too."""

        default_permissions = ("add", "view")
        permissions = (("approve_payout", "Can approve payout"),)

    @classmethod
    def send(cls, payee: str, iban: str, amount: Money) -> Self:
        """Send ``amount`` to ``payee``'s account ``iban``."""
        iban = compact_iban(iban)
        if not payee.strip():
            raise InvalidPayout("Name the payee.")
        if not iban_holds(iban):
            raise InvalidPayout("This is not an IBAN.")
        if amount.amount <= 0:
            raise InvalidPayout("A payout moves more than nothing.")
        payout = cls(
            id=PayoutId(uuid4()), payee=payee.strip(), iban=iban, amount=amount
        )
        payout.record(
            PayoutSent(payout_id=payout.id, payee=payout.payee, amount=amount)
        )
        return payout


class PayoutRepository(Repository[Payout, PayoutId]):
    """Every payout: ``get``, ``add``, ``filter``, ``paginate``, ..."""

    def not_found(self, id_: PayoutId) -> PayoutNotFound:
        """Report a missing payout."""
        return PayoutNotFound(f"Payout {id_} does not exist.")


class PayoutsUnitOfWork(UnitOfWork, ABC):
    """One transaction over the payouts domain's repositories."""

    payouts: PayoutRepository


def compact_iban(iban: str) -> str:
    """``iban`` as banks store it: without spaces, in capitals."""
    return iban.replace(" ", "").upper()


def iban_holds(iban: str) -> bool:
    """Whether ``iban``, compact, is an IBAN whose check digits hold (ISO 13616).

    The country and check digits move to the end, each letter becomes a
    number (A is 10, Z is 35), and the whole number leaves 1 divided by 97.
    """
    if not (
        SHORTEST_IBAN <= len(iban) <= LONGEST_IBAN
        and iban.isascii()
        and iban.isalnum()
        and iban[:2].isalpha()
        and iban[2:4].isdigit()
    ):
        return False
    digits = "".join(str(int(char, 36)) for char in iban[4:] + iban[:4])
    return int(digits) % 97 == 1
