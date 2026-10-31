"""The hook other apps register on the ledger's posting path (ADR 0101).

An app that must see every entry before it is booked registers in its
``ready()``::

    from domainkit.contrib.accounting import on_posting

    class LimitsConfig(DomainConfig):
        def ready(self) -> None:
            on_posting(enforce_limits)

The hook runs inside the posting's transaction, after its accounts and
period are locked and their balances moved, before the entry is added:
for every entry ``post_entry``, ``reverse_entry``, and ``capture_hold``
book. Raising refuses the entry, and its transaction rolls back with
whatever the hook wrote. Rows a hook locks come after the accounts in the
ledger's lock order, so take them in an order of their own (ADR 0116).
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from domainkit.apps.hooks import HookPoint

if TYPE_CHECKING:
    from .models import Account, AccountId, Entry
    from .uow import AccountingUnitOfWork

type PostingHook = Callable[
    ["AccountingUnitOfWork", "Entry", Mapping["AccountId", "Account"]],
    Awaitable[None],
]

on_posting: HookPoint[PostingHook] = HookPoint(
    "accounting.posting", arguments=3, awaited=True
)
"""See every entry before it is booked: ``(uow, entry, accounts)``; raise to refuse it.

``accounts`` are the entry's accounts, locked, their balances moved by it.
"""
