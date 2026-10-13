"""Pages of results for keyset ("cursor") pagination.

A use case asks the repository for ``limit + 1`` items after a position and
returns ``Page.from_items``. The extra item only tells whether another page
exists. Keyset pagination stays fast on deep pages and has no unbounded
``offset`` for clients to abuse.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Up to ``limit`` items and whether more follow the last one."""

    items: list[T]
    has_more: bool

    @classmethod
    def from_items(cls, items: Sequence[T], limit: int) -> Self:
        """Build a page from ``limit + 1`` fetched items."""
        return cls(items=list(items[:limit]), has_more=len(items) > limit)
