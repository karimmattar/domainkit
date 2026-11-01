"""PERF-25: reconciling by amount and date compares every line with every posting.

``ByAmountAndDate.match`` (a default of ``ACCOUNTING.MATCHING_RULES``)
builds each line's candidates by testing it against every unmatched
posting of the account (``_near``): lines x postings comparisons, in
Python, on the event loop, inside the transaction that holds the
account's reconcile lock. Measured: 1,000 lines and postings 0.07 s,
4,000 1.4 s, 8,000 6.1 s; one statement of ``MAX_STATEMENT_LINES``
(10,000) is about 10 s and two about 40 s, during which the process
serves no other request (``POST /accounts/{id}/reconcile``).

The right behaviour: candidates are found by amount and currency (a dict
or a sort), so the work grows with the lines and postings, not their
product. Counted, not timed: ``_near`` is called a bounded number of
times per line.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

import pytest

from domainkit.contrib.accounting import reconciliation
from domainkit.contrib.accounting.reconciliation import (
    ByAmountAndDate,
    PostingItem,
    StatementItem,
)

ITEMS: Final = 2_000
START: Final = date(2026, 10, 1)


def test_amount_and_date_matching_is_not_lines_times_postings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2,000 lines and 2,000 postings, all of different amounts: few comparisons."""
    lines = [
        StatementItem(
            uuid.uuid4(), START + timedelta(days=n % 30), Decimal(n + 1), "USD", "", ""
        )
        for n in range(ITEMS)
    ]
    postings = [
        PostingItem(
            uuid.uuid4(),
            uuid.uuid4(),
            START + timedelta(days=n % 30),
            Decimal(n + 1),
            "USD",
            f"r{n}",
            "",
        )
        for n in range(ITEMS)
    ]
    calls = 0
    near = reconciliation._near

    def counted(*args: object) -> bool:
        nonlocal calls
        calls += 1
        return near(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(reconciliation, "_near", counted)

    pairs = list(ByAmountAndDate().match(lines, postings, timedelta(days=3)))

    assert len(pairs) == ITEMS
    assert calls <= 10 * ITEMS, f"{calls} comparisons for {ITEMS} lines"
