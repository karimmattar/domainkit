"""What the payouts domain does: send a payout, as one act its maker confirmed.

``send_payout`` is guarded twice:

- **Approval** (``requires_approval``): over ``THRESHOLD`` it runs only once
  another person approved it. Its guard is registered in ``apps.py``.
- **The act** (``SEND``): the payout route asks its maker for an
  authorization of this payout, with these arguments, confirmed with a code,
  and spends it once (``requires_authorization``, in ``routes.py``).

A large payout is submitted at approvals' ``POST /api/approvals/requests``,
whose act (``approvals.submit``) shows the maker the same lines, and it is
approved with an act of the approver's own (``approvals.approve``).
"""

from typing import Final

from domainkit.contrib.approvals import Threshold, requires_approval
from domainkit.contrib.auth.actions import Action, Shown
from domainkit.ddd import Command
from domainkit.money import Money

from domains.payouts.domain import Iban, Payee, Payout, PayoutsUnitOfWork

SEND_PAYOUT: Final = "payouts.send_payout"
"""The guarded action, and the purpose of the act that confirms it."""
THRESHOLD: Final = Threshold({"USD": "10000.00", "EUR": "10000.00"})
"""Over this, a payout waits for an approver; a currency it does not name
always does (a threshold fails closed)."""


class SendPayout(Command):
    """Send ``amount`` to ``payee``'s account ``iban``."""

    payee: Payee
    iban: Iban
    amount: Money


@requires_approval(SEND_PAYOUT)
async def send_payout(command: SendPayout, uow: PayoutsUnitOfWork) -> Payout:
    """Send the payout; over the threshold, only as an approved request's run."""
    payout = Payout.send(command.payee, command.iban, command.amount)
    async with uow:
        await uow.payouts.add(payout)
        await uow.commit()
    return payout


def large(command: SendPayout) -> bool:
    """Whether the payout is over the threshold of its currency."""
    return THRESHOLD.exceeded_by([command.amount])


def shown(command: SendPayout) -> dict[str, str]:
    """What a person deciding on the payout reads: how much, to whom, where."""
    amount = f"{command.amount.amount} {command.amount.currency.code}"
    return {"Pay": f"{amount} to {command.payee}", "IBAN": command.iban}


SEND: Final = Action(
    SEND_PAYOUT,
    command=SendPayout,
    subject=lambda command: "payouts.payout:new",
    show=lambda command: Shown("Send a payout", shown(command)),
)
"""The act a maker confirms on the payout route: this payout, once."""
