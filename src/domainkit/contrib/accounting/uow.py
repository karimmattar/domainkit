"""The unit of work over the ledger, and its repositories.

Every change to the books takes its locks in one order, so two changes
never wait for each other: the name of what it posts (``lock``), the
period its date falls in (shared), holds, then accounts by ascending id
(``get_many_for_update``). On SQLite the first lock takes the database's
write lock, so changes run one at a time there.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


class EntryRepository(SqlAlchemyRepository[Entry, EntryId]):
    """Journal entries, with their postings."""

    model = Entry

    def not_found(self, id_: EntryId) -> NotFound:
        """The error for a missing entry."""
        return EntryNotFound("No journal entry has that id.")

    async def by_reference(self, source: str, reference: str) -> Entry | None:
        """The entry ``source`` posted as ``reference``, if any."""
        found = await self.session.scalar(
            select(Entry).where(
                entries.c.source == source, entries.c.reference == reference
            )
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
        statement = (
            select(Period)
            .where(periods.c.starts_on <= day, periods.c.ends_on >= day)
            .execution_options(populate_existing=True)
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=True)
        found = await self.session.scalar(statement)
        if found is not None:
            self._track(found)
        return found

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
