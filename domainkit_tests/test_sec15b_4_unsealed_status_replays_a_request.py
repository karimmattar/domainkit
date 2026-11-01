"""SEC-15b-4: a request's status is not sealed, so a row edit runs it again.

The docs: "A row changed or added in the database by someone without
the key runs nothing". The seals cover the request's id, action,
fingerprint, maker, and approvals needed, and each decision; they do not
cover ``status`` (nor ``permission``, ``group``, ``expires_at``). A
database writer without ``SECRET_KEY`` sets an executed request back to
``approved`` and queues ``domainkit.approvals.execute`` for it again (a
new outbox row, so the inbox does not dedupe it): every check of
``execute_request`` holds, and the approved command runs a second time.
The same edit revives a rejected, cancelled, or expired request for new
approvals, or names approvers by another ``permission``.

The test asserts the safe behaviour (the use case runs once) and fails
on ``d23a2ec``.
"""

import uuid

import pytest
from sqlalchemy import update

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    OpenAccount,
    open_account,
)
from domainkit.contrib.accounting.tables import accounts
from domainkit.contrib.approvals import (
    Policy,
    RequestStatus,
    execute_request,
    guard,
    requires_approval,
    submit_command,
)
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.ddd import Command
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    approve,
    count,
    project,
    stored,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]


class PayOut(Command):
    """A payout: each run books a new one (a project's non-idempotent use case)."""

    payee: str


@requires_approval("tests.pay_out")
async def pay_out(command: PayOut, uow: AccountingUnitOfWork) -> None:
    """Book the payout; stands in for a refund or a transfer to a provider."""
    await open_account(
        OpenAccount(
            code=f"po-{uuid.uuid4().hex[:12]}",
            name=f"Payout {command.payee}",
            type=AccountType.ASSET,
            currency="USD",
        ),
        uow,
    )


async def test_an_executed_request_set_back_to_approved_does_not_run_again() -> None:
    """Approved and run once; its status edited in the database; delivered again."""
    guard(
        "tests.pay_out",
        pay_out,
        unit_of_work=AccountingUnitOfWork,
        policies=[Policy("tests.payouts", permission="accounting.add_account")],
        domain="alpha",
    )
    request = await submit_command("tests.pay_out", PayOut(payee="eve"), maker="eve")
    await approve(request, "ann")
    await drain()
    assert (await stored(request.id)).status is RequestStatus.EXECUTED
    assert await count(accounts, accounts.c.name == "Payout eve") == 1

    # Someone with the database, not the key: the row says it waits to run.
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(requests)
            .where(requests.c.id == request.id)
            .values(status=RequestStatus.APPROVED, executed_at=None)
        )
    await execute_request(request.id)  # As a message queued again would.

    assert await count(accounts, accounts.c.name == "Payout eve") == 1, (
        "an approved command ran twice after a row edit no seal covers"
    )
