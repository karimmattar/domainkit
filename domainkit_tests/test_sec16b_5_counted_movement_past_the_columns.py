"""SEC-16b-5: a movement limits count past ``NUMERIC(18, 4)`` fails as a server error.

FIN-91 made the ledger refuse a line, a balance, or a held amount past
its columns as ``amount_out_of_range`` (422). Limits count a movement
per subject as the *sum* of an entry's postings for it (``movements``):
an actor's movement is every debit of the entry, an account's or a
holder's every posting to it. Each line and each balance fits, but the
sum need not: four lines of 60,000,000,000,000 (14 digits, below the
ledger's 10^14) make an actor movement of 1.2 x 10^14, which
``_record`` writes into ``usage.amount``, ``ExactDecimal(18, 4)``: the
flush raises ``StatementError`` (a 500 over HTTP; retried by the worker
from ``defer_entry``, since it is not one of ``tasks.REFUSALS``) instead
of a refusal. It needs a window that bounds the actor without an amount
(a velocity limit, ``max_count``, as FIN-98 recommends for staff); with
``max_amount`` the window refuses first.

The test asserts the safe behaviour (a ``DomainError``, as FIN-91 made
the ledger's own refusals) and fails on ``675dfd6``.
"""

import contextlib

import pytest

from domainkit.context import RequestContext, bind_request
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    PostEntry,
    post_entry,
)
from domainkit.contrib.limits import Window
from domainkit.ddd import DomainError
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    TODAY,
    account,
    books,
    egp,
    limit,
    period,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]
LINE = "60000000000000"
"""Fourteen digits: one line, and every balance, fits the ledger's columns."""


async def test_an_entry_whose_actor_movement_overflows_is_refused_not_a_500() -> None:
    """Two debits and two credits of 6 x 10^13 by an actor a velocity limit bounds."""
    await period()
    a, b = await account("a"), await account("b")
    c = await account("c", AccountType.INCOME)
    d = await account("d", AccountType.INCOME)
    await limit(code="staff.velocity", kind="actor", window=Window.DAY, max_count=100)
    entry = PostEntry(
        source="treasury",
        reference="big-1",
        booked_on=TODAY,
        lines=(
            Line.debit(a.id, egp(LINE)),
            Line.debit(b.id, egp(LINE)),
            Line.credit(c.id, egp(LINE)),
            Line.credit(d.id, egp(LINE)),
        ),
    )
    # Refused as such: what the ledger promises (FIN-91).
    with (
        bind_request(RequestContext("r-1", None, "POST", "/", actor="clerk-1")),
        contextlib.suppress(DomainError),
    ):
        await post_entry(entry, AccountingUnitOfWork())
