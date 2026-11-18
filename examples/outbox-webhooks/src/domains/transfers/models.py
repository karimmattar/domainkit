"""How the transfers domain is stored, and how it reaches the bank.

``table_for`` makes the table from the aggregate's fields. ``HttpBank``
calls the bank's API through ``domainkit.http.client``: pooled, bounded by
the work's deadline, behind a circuit breaker, and stubbed in tests. A
failed call is not tried again here: the worker runs the whole message
again later, which is the one place retries are counted. ``send_transfer``
is the durable handler ``apps.py`` subscribes: it runs ``send_to_bank``
with these adapters, under the inbox.
"""

from domainkit.apps import registry
from domainkit.contrib.messaging import inbox
from domainkit.db import SqlAlchemyUnitOfWork, map_imperatively, table_for
from domainkit.http.client import HttpClient
from pydantic import BaseModel

from domains.transfers import controllers
from domains.transfers.domain import (
    Bank,
    Transfer,
    TransferRequested,
    TransfersUnitOfWork,
)

transfers = table_for(Transfer, "transfers_transfers", domain="transfers")
bank_api = HttpClient(name="bank", timeout=10.0)


class TransfersSettings(BaseModel):
    """The domain's settings, ``TRANSFERS`` (validated at startup)."""

    BANK_URL: str
    """The bank's API, which transfers are sent to."""


class SqlAlchemyTransfersUnitOfWork(SqlAlchemyUnitOfWork, TransfersUnitOfWork):
    """One database transaction over the transfers repositories."""


class HttpBank(Bank):
    """The bank's API, at ``TRANSFERS["BANK_URL"]``."""

    async def send(self, transfer: Transfer) -> str:
        """POST the transfer, keyed by its id; the bank's reference."""
        response = await bank_api.post(
            f"{registry.settings(TransfersSettings).BANK_URL}/transfers",
            json={
                "payee": transfer.payee,
                "iban": transfer.iban,
                "amount": str(transfer.amount.amount),
                "currency": transfer.amount.currency.code,
            },
            headers={"Idempotency-Key": str(transfer.id)},
        )
        response.raise_for_status()  # A failure: the worker tries again later.
        reference: str = response.json()["reference"]
        return reference


@inbox
async def send_transfer(event: TransferRequested) -> None:
    """Send a requested transfer to the bank (the durable handler)."""
    await controllers.send_to_bank(
        SqlAlchemyTransfersUnitOfWork(), HttpBank(), event.transfer_id
    )


def start_mappers() -> None:
    """Map the aggregates onto their tables (idempotent)."""
    map_imperatively(Transfer, transfers)
