"""The unit of work over the limits, and where they live."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.db.routing import router
from domainkit.ddd import NotFound

from .exceptions import LimitNotFound
from .models import Direction, Limit, LimitId
from .tables import limits


def limits_alias() -> str:
    """The SQL alias holding the limits and their usage: one database."""
    return router.require_standalone(
        router.target_for("limits"), "DATABASE_ROUTING['limits']"
    )


class LimitRepository(SqlAlchemyRepository[Limit, LimitId]):
    """The limits."""

    model = Limit

    def not_found(self, id_: LimitId) -> NotFound:
        """The error for a missing limit."""
        return LimitNotFound("No limit has that id.")

    async def by_key(
        self, code: str, kind: str, direction: Direction, currency: str, subject: str
    ) -> Limit | None:
        """The limit with that key, if any."""
        found = await self.session.scalar(
            select(Limit).where(
                limits.c.code == code,
                limits.c.kind == kind,
                limits.c.direction == direction,
                limits.c.currency == currency,
                limits.c.subject == subject,
            )
        )
        if found is not None:
            self._track(found)
        return found


class LimitsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the limits, wherever ``limits`` is routed."""

    limits: LimitRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository to the session."""
        self.limits = LimitRepository(session)
