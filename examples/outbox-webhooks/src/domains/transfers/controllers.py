"""What the transfers domain does: request a transfer, then send it to the bank.

A request commits the transfer and its ``TransferRequested`` event in one
transaction; the event's durable handler is written to the outbox in that
same transaction. So the bank hears of a transfer only once it committed,
and always does, even if the process dies right after the commit: the
worker runs ``send_to_bank`` from the outbox, again after a failure.
"""

from uuid import UUID

from domainkit.ddd import use_case
from domainkit.money import Money

from domains.transfers.domain import (
    Bank,
    Iban,
    Payee,
    Transfer,
    TransferId,
    TransfersUnitOfWork,
)


@use_case
async def request_transfer(
    uow: TransfersUnitOfWork, payee: Payee, iban: Iban, amount: Money
) -> Transfer:
    """Request a transfer of ``amount`` to ``payee``'s account ``iban``."""
    transfer = Transfer.request(payee, iban, amount)
    await uow.transfers.add(transfer)
    return transfer


async def send_to_bank(uow: TransfersUnitOfWork, bank: Bank, transfer_id: UUID) -> None:
    """Hand a requested transfer to the bank, then record that it was sent.

    No transaction is open while the bank answers. The worker may run this
    twice (it died after the bank's answer, before the commit): the bank
    keys the transfer by its id, so the second call moves nothing more, and
    the handler's inbox keeps its commit to one.
    """
    async with uow:
        transfer = await uow.transfers.get(TransferId(transfer_id))
    reference = await bank.send(transfer)
    async with uow:
        transfer = await uow.transfers.get(TransferId(transfer_id))
        transfer.mark_sent(reference)
        await uow.commit()
