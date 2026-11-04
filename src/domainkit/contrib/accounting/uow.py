"""The unit of work over the ledger, and its repositories.

Every change to the books takes its locks in one order, so no two wait
for each other: the name of what it posts (``lock``), then a hold, then
accounts by ascending id (``get_many_for_update``), then, last and
shared, the period its date falls in. On SQLite the first lock takes the
database's write lock, so changes run one at a time there.
"""

from datetime import date
from functools import cache
from typing import Any, Final

from sqlalchemy import Select, bindparam, exists, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.db.routing import router
from domainkit.ddd import NotFound

from .exceptions import AccountNotFound, EntryNotFound, HoldNotFound, PeriodNotFound
from .models import (
    Account,
    AccountId,
    Entry,
    EntryId,
    Hold,
    HoldId,
    Period,
    PeriodId,
    PeriodStatus,
)
from .tables import entries, holds, periods


def accounting_alias() -> str:
    """The SQL alias holding the ledger: one database, never a shard group."""
    return router.require_standalone(
        router.target_for("accounting"), "DATABASE_ROUTING['accounting']"
    )


class AccountRepository(SqlAlchemyRepository[Account, AccountId]):
    """The chart of accounts."""

    model = Account

    def not_found(self, id_: AccountId) -> NotFound:
        """The error for a missing account."""
        return AccountNotFound("No account has that id.")


CLOSED_THROUGH: Final = select(func.max(periods.c.ends_on)).where(
    periods.c.status == PeriodStatus.CLOSED
)
"""The last day of the latest closed period, made once: a statement made once
keeps its cache key, which SQLAlchemy walks a new one for."""


@cache
def _entry_by_reference(mapper: Mapper[Entry]) -> "Select[Any]":
    """``by_reference``'s statement, made once per mapping of ``Entry``."""
    return select(mapper).where(
        entries.c.source == bindparam("source", type_=entries.c.source.type),
        entries.c.reference == bindparam("reference", type_=entries.c.reference.type),
    )


@cache
def _covering(mapper: Mapper[Period], share: bool) -> "Select[Any]":
    """``covering``'s statement, made once per mapping of ``Period``.

    ``share`` locks the period for share (PostgreSQL; SQLite has no row locks).
    """
    day = bindparam("day", type_=periods.c.starts_on.type)
    statement = (
        select(mapper)
        .where(periods.c.starts_on <= day, periods.c.ends_on >= day)
        .execution_options(populate_existing=True)
    )
    return statement.with_for_update(read=True) if share else statement


class EntryRepository(SqlAlchemyRepository[Entry, EntryId]):
    """Journal entries, with their postings."""

    model = Entry

    def not_found(self, id_: EntryId) -> NotFound:
        """The error for a missing entry."""
        return EntryNotFound("No journal entry has that id.")

    async def by_reference(self, source: str, reference: str) -> Entry | None:
        """The entry ``source`` posted as ``reference``, if any."""
        found: Entry | None = await self.session.scalar(
            _entry_by_reference(inspect(Entry)),
            {"source": source, "reference": reference},
        )
        if found is not None:
            self._track(found)
        return found


class HoldRepository(SqlAlchemyRepository[Hold, HoldId]):
    """Holds on accounts."""

    model = Hold

    def not_found(self, id_: HoldId) -> NotFound:
        """The error for a missing hold."""
        return HoldNotFound("No hold has that id.")

    async def by_reference(self, source: str, reference: str) -> Hold | None:
        """The hold ``source`` placed as ``reference``, if any."""
        found = await self.session.scalar(
            select(Hold).where(holds.c.source == source, holds.c.reference == reference)
        )
        if found is not None:
            self._track(found)
        return found


class PeriodRepository(SqlAlchemyRepository[Period, PeriodId]):
    """Accounting periods."""

    model = Period

    def not_found(self, id_: PeriodId) -> NotFound:
        """The error for a missing period."""
        return PeriodNotFound("No accounting period has that id.")

    async def covering(self, day: date) -> Period | None:
        """The period ``day`` falls in, locked for share until the transaction ends.

        A posting takes it shared, so postings run side by side while a
        close (which locks it for update) waits for them, and the postings
        after it see it closed. SQLite has no row locks.
        """
        dialect = self.session.get_bind().dialect.name
        statement = _covering(inspect(Period), dialect == "postgresql")
        found: Period | None = await self.session.scalar(statement, {"day": day})
        if found is not None:
            self._track(found)
        return found

    async def closed_through(self) -> date | None:
        """The last day of the latest closed period: the books are closed up to it."""
        found: date | None = await self.session.scalar(CLOSED_THROUGH)
        return found

    async def open_before(self, day: date) -> bool:
        """Whether an open period starts before ``day``."""
        return bool(
            await self.session.scalar(
                select(
                    exists().where(
                        periods.c.status == PeriodStatus.OPEN,
                        periods.c.starts_on < day,
                    )
                )
            )
        )

    async def overlapping(self, starts_on: date, ends_on: date) -> bool:
        """Whether a period covers any day from ``starts_on`` to ``ends_on``."""
        found = await self.session.scalar(
            select(periods.c.id)
            .where(periods.c.starts_on <= ends_on, periods.c.ends_on >= starts_on)
            .limit(1)
        )
        return found is not None


class AccountingUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the ledger, wherever ``accounting`` is routed."""

    accounts: AccountRepository
    entries: EntryRepository
    holds: HoldRepository
    periods: PeriodRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repositories to the session."""
        self.accounts = AccountRepository(session)
        self.entries = EntryRepository(session)
        self.holds = HoldRepository(session)
        self.periods = PeriodRepository(session)
