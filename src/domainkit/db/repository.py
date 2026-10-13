"""Repository adapter backed by an SQLAlchemy ``AsyncSession``."""

from collections.abc import Hashable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.ddd import AggregateRoot, Repository


class SqlAlchemyRepository[AggregateT: AggregateRoot[Any], IdT: Hashable](
    Repository[AggregateT, IdT]
):
    """Store one imperatively mapped aggregate type; set ``model`` in subclasses."""

    model: type[AggregateT]

    def __init__(self, session: AsyncSession) -> None:
        """Work inside ``session``, which the unit of work owns."""
        super().__init__()
        self.session = session

    async def _add(self, aggregate: AggregateT) -> None:
        """Add ``aggregate`` to the session."""
        self.session.add(aggregate)

    async def _get(self, id_: IdT) -> AggregateT | None:
        """Load by primary key."""
        return await self.session.get(self.model, id_)

    async def _remove(self, aggregate: AggregateT) -> None:
        """Mark ``aggregate`` for deletion."""
        await self.session.delete(aggregate)
