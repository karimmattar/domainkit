"""Hook points the ledger offers other apps.

``on_posting`` checks an entry before ``post_entry`` posts it: register
``async def hook(command: PostEntry)`` in ``DomainConfig.ready()``; a hook
that raises refuses the entry, before any lock is taken.
``domainkit.contrib.approvals`` refuses there the entries over
``APPROVALS.ENTRY_THRESHOLDS`` that no approved request posts.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from domainkit.apps.hooks import HookPoint

if TYPE_CHECKING:
    from .ledger import PostEntry

__all__ = ["on_posting"]

type PostingHook = Callable[["PostEntry"], Awaitable[None]]

on_posting: HookPoint[PostingHook] = HookPoint("posting", arguments=1, awaited=True)
"""Check an entry before ``post_entry`` posts it: ``(command)``; raising refuses it."""
