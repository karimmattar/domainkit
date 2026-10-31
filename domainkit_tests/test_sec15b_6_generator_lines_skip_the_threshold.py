"""SEC-15b-6: ``post_entry`` with lines given as an iterator posts unapproved.

The docs: over ``ENTRY_THRESHOLDS``, ``post_entry`` refuses an entry
"whether called over HTTP, from code, or from a message". Commands are
not validated when built in code, so ``PostEntry(lines=<generator>)``
keeps the generator. ``post_entry`` first reads it whole
(``lines = tuple(command.lines)``), then calls the ``on_posting`` hooks
with the command; the approvals guard sums ``command.lines`` again,
finds the generator spent, sees no debit, and lets the entry through.
The ledger then books the tuple it read: 5,000 dollars, no approval.
A project service that builds its lines with a generator expression
(``lines=(Line.debit(...) for item in batch)``) silently loses the
guard for everything it posts.

The test asserts the safe behaviour (``approval_required``, nothing
booked) and fails on ``d23a2ec``.
"""

import pytest

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    Line,
    PostEntry,
    post_entry,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.approvals import ApprovalRequired
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    TODAY,
    books,
    count,
    project,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]


async def test_lines_from_a_generator_are_guarded_as_a_tuple_is() -> None:
    """Threshold 1,000 dollars; a 5,000 sale whose lines a generator yields."""
    cash, sales = await books()
    lines = [Line.debit(cash.id, usd("5000.00")), Line.credit(sales.id, usd("5000.00"))]
    large = PostEntry(
        source="shop",
        reference="g-1",
        booked_on=TODAY,
        lines=(line for line in lines),  # type: ignore[arg-type]
    )
    try:
        await post_entry(large, AccountingUnitOfWork())
    except ApprovalRequired:
        pass  # The safe behaviour.
    assert await count(entries, entries.c.reference == "g-1") == 0, (
        "a 5,000 entry over the 1,000 threshold was posted without approval"
    )
