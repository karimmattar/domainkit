"""Hook points the ledger offers other apps (ADR 0101).

Register in ``DomainConfig.ready()``; each is called in turn, and a hook
that raises refuses the entry. They differ in when they run:

- ``on_posting`` checks an entry before ``post_entry`` posts it:
  ``async def hook(command: PostEntry)``, before any lock is taken and only
  for ``post_entry``. ``domainkit.contrib.approvals`` refuses there the
  entries over ``APPROVALS.ENTRY_THRESHOLDS`` that no approved request
  posts.
- ``on_booking`` sees every entry as it is booked:
  ``async def hook(uow, entry, accounts)``, inside the posting's
  transaction, after its accounts and period are locked and their balances
  moved, before the entry is added, for every entry ``post_entry``,
  ``reverse_entry``, and ``capture_hold`` book. The transaction rolls back
  with whatever the hook wrote. Rows a hook locks come after the accounts
  in the ledger's lock order, so take them in an order of their own
  (ADR 0116). ``domainkit.contrib.limits`` counts each entry there
  (ADR 0123)::

      from domainkit.contrib.accounting import on_booking

      class LimitsConfig(DomainConfig):
          def ready(self) -> None:
              on_booking(enforce_limits)
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from domainkit.apps.hooks import HookPoint

if TYPE_CHECKING:
    from .ledger import PostEntry
    from .models import Account, AccountId, Entry
    from .uow import AccountingUnitOfWork

__all__ = ["on_booking", "on_posting"]

type PostingHook = Callable[["PostEntry"], Awaitable[None]]
type BookingHook = Callable[
    ["AccountingUnitOfWork", "Entry", Mapping["AccountId", "Account"]],
    Awaitable[None],
]

on_posting: HookPoint[PostingHook] = HookPoint("posting", arguments=1, awaited=True)
"""Check an entry before ``post_entry`` posts it: ``(command)``; raising refuses it."""
on_booking: HookPoint[BookingHook] = HookPoint(
    "accounting.booking", arguments=3, awaited=True
)
"""See every entry as it is booked: ``(uow, entry, accounts)``; raise to refuse it.

``accounts`` are the entry's accounts, locked, their balances moved by it.
"""
