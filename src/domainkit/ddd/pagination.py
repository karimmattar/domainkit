"""Pages of results: keyset ("cursor") pages, or offset pages.

``Repository.paginate`` returns a ``Page``: up to ``limit`` aggregates in
the query's ordering (the id breaks ties), whether more follow, and the
position of the last one, which the next page continues after::

    first = await uow.orders.paginate(query, limit=20)
    second = await uow.orders.paginate(query, limit=20, after=first.next_position)

Keyset pages stay fast on deep pages and have no unbounded offset for
clients to abuse; ``offset=`` is there for the pages a user can jump to.
``count=True`` also counts every match (``total``), which costs a query.

A hand-written use case may still fetch ``limit + 1`` items and call
``Page.from_items``; the extra item only tells whether another page exists.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Up to ``limit`` items and whether more follow the last one."""

    items: list[T]
    has_more: bool
    next_position: tuple[object, ...] | None = None
    """The last item's values of the ordering's fields, if more follow."""
    total: int | None = None
    """How many items match in all, when counted."""

    @classmethod
    def from_items(cls, items: Sequence[T], limit: int) -> Self:
        """Build a page from ``limit + 1`` fetched items."""
        return cls(items=list(items[:limit]), has_more=len(items) > limit)
