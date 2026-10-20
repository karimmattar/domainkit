"""The unit of work over flags and dynamic settings."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork

from .models import DynamicSetting, Flag
from .tables import flags


class FlagRepository(SqlAlchemyRepository[Flag, str]):
    """Flags by name."""

    model = Flag

    async def all(self) -> list[Flag]:
        """Every flag, by name."""
        found = list(await self.session.scalars(select(Flag).order_by(flags.c.id)))
        for flag in found:
            self._track(flag)
        return found


class DynamicSettingRepository(SqlAlchemyRepository[DynamicSetting, str]):
    """Stored dynamic settings by key."""

    model = DynamicSetting


class FlagsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the flags tables, wherever ``flags`` is routed."""

    flags: FlagRepository
    settings: DynamicSettingRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repositories to the session."""
        self.flags = FlagRepository(session)
        self.settings = DynamicSettingRepository(session)
