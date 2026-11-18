"""The business of the transfers domain: the ``Transfer`` aggregate and its ports.

Plain Python: only ``domainkit.ddd``, ``domainkit.money``, and the standard
library. A transfer is requested in a request, and sent to the bank later,
by the worker: the ``Bank`` port is what the business needs of it.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Annotated, NewType, Self
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

TransferId = NewType("TransferId", UUID)
Payee = Annotated[str, MaxLength(100)]
Iban = Annotated[str, MaxLength(34)]


class TransferNotFound(NotFound):
    """No transfer has the requested id."""


class InvalidTransfer(BusinessRuleViolation):
    """A transfer names its payee and moves more than nothing."""


class TransferStatus(StrEnum):
    """Where a transfer is."""

    REQUESTED = "requested"
    SENT = "sent"


class TransferRequested(DomainEvent):
    """A transfer was requested: the worker sends it to the bank."""

    transfer_id: TransferId


class TransferSent(DomainEvent):
    """The bank took a transfer: subscribers hear of it (``transfer.sent``)."""

    transfer_id: TransferId
    amount: Money
    bank_reference: str


class Transfer(AggregateRoot[TransferId]):
    """Money sent from the business to a payee's bank account."""

    payee: Payee
    iban: Iban
    amount: Annotated[Money, Digits(14, 2)]
    status: TransferStatus = TransferStatus.REQUESTED
    bank_reference: Annotated[str, MaxLength(64)] | None = None

    @classmethod
    def request(cls, payee: str, iban: str, amount: Money) -> Self:
        """Ask for ``amount`` to go to ``payee``'s account ``iban``."""
        if not payee.strip():
            raise InvalidTransfer("Name the payee.")
        if amount.amount <= 0:
            raise InvalidTransfer("A transfer moves more than nothing.")
        transfer = cls(
            id=TransferId(uuid4()), payee=payee.strip(), iban=iban, amount=amount
        )
        transfer.record(TransferRequested(transfer_id=transfer.id))
        return transfer

    def mark_sent(self, bank_reference: str) -> None:
        """Record that the bank took it, under ``bank_reference``; once."""
        if self.status is TransferStatus.SENT:
            return
        self.status, self.bank_reference = TransferStatus.SENT, bank_reference
        self.record(
            TransferSent(
                transfer_id=self.id, amount=self.amount, bank_reference=bank_reference
            )
        )


class TransferRepository(Repository[Transfer, TransferId]):
    """Every transfer: ``get``, ``add``, ``filter``, ``paginate``, ..."""

    def not_found(self, id_: TransferId) -> TransferNotFound:
        """Report a missing transfer."""
        return TransferNotFound(f"Transfer {id_} does not exist.")


class TransfersUnitOfWork(UnitOfWork, ABC):
    """One transaction over the transfers domain's repositories."""

    transfers: TransferRepository


class Bank(ABC):
    """The bank that carries transfers out (a port; models.py calls it)."""

    @abstractmethod
    async def send(self, transfer: Transfer) -> str:
        """Hand ``transfer`` to the bank; the bank's reference for it.

        Sent twice, the same transfer moves money once: the bank keys it
        by the transfer's id.
        """
