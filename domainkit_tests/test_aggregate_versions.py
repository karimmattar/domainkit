"""An aggregate's table needs a version column, or the mapping says why not.

Without one, concurrent changes to a row overwrite each other with no error
(audit 0.9.0, FIN-77): an account withdrawn from by ten requests at once
kept nine withdrawals' money. ``map_imperatively`` now refuses that table
unless the mapping opts out with ``versioned=False``.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from typing import Annotated, NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    select,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import (
    ExactDecimal,
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
)
from domainkit.db.hooks import on_stage
from domainkit.db.query import mapper_of
from domainkit.ddd import AggregateRoot, ConcurrentUpdate, Digits, Entity, Retry
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import ALPHA

pytestmark = pytest.mark.anyio

AccountId = NewType("AccountId", UUID)
money = MetaData()

WRITERS = 10
AMOUNT = Decimal("10.00")
START = Decimal("1000.00")


class Account(AggregateRoot[AccountId]):
    """A balance, on a hand-written table as docs/databases.md shows it."""

    balance: Annotated[Decimal, Digits(18, 2)]

    def withdraw(self, amount: Decimal) -> None:
        """Take ``amount`` off the balance."""
        self.balance -= amount


class Tally(AggregateRoot[AccountId]):
    """A balance whose table has no version column, mapped so on purpose."""

    balance: Annotated[Decimal, Digits(18, 2)]


accounts = Table(
    "framework_test_accounts",
    money,
    Column("id", Uuid, primary_key=True),
    Column("balance", ExactDecimal(18, 2), nullable=False),
    Column("version", Integer, nullable=False),
)
tallies = Table(
    "framework_test_tallies",
    money,
    Column("id", Uuid, primary_key=True),
    Column("balance", ExactDecimal(18, 2), nullable=False),
)
map_imperatively(Account, accounts)
map_imperatively(Tally, tallies, versioned=False)


class Accounts(SqlAlchemyRepository[Account, AccountId]):
    """Accounts."""


class Tallies(SqlAlchemyRepository[Tally, AccountId]):
    """Tallies."""


class BankUnitOfWork(SqlAlchemyUnitOfWork):
    """Both repositories."""

    accounts: Accounts
    tallies: Tallies

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repositories."""
        self.accounts = Accounts(session)
        self.tallies = Tallies(session)


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database with room for every writer's connection."""
    with override_settings(
        DATABASES={"default": {"URL": database_url, "POOL_SIZE": WRITERS}}
    ):
        yield database_url


@pytest.fixture
async def tables(database: str) -> None:
    """The money tables."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(money.create_all)


def unversioned(name: str) -> Table:
    """A table of ``name`` with an id and nothing else."""
    return Table(name, MetaData(), Column("id", Integer, primary_key=True))


def test_an_aggregate_table_without_a_version_is_refused() -> None:
    """The message names the class, the table, the column and the opt-out."""

    class Wallet(AggregateRoot[int]):
        pass

    with pytest.raises(ImproperlyConfigured) as caught:
        map_imperatively(Wallet, unversioned("framework_test_wallets"))

    assert str(caught.value) == (
        "domainkit_tests.test_aggregate_versions."
        "test_an_aggregate_table_without_a_version_is_refused.<locals>.Wallet is an "
        "aggregate, but its table 'framework_test_wallets' has no 'version' column, "
        "so concurrent changes to one row would overwrite each other without an "
        'error. Add Column("version", Integer, nullable=False) to the table '
        '(server_default="0" in the migration for existing rows), or pass '
        "versioned=False to map_imperatively to map it without optimistic locking "
        "on purpose."
    )
    assert sa_inspect(Wallet, raiseerr=False) is None


def test_versioned_false_maps_without_optimistic_locking() -> None:
    """The opt-out maps the aggregate, and its own version column stays unused."""

    class Ledger(AggregateRoot[int]):
        pass

    table = unversioned("framework_test_ledgers")
    table.append_column(Column("version", Integer, nullable=False))
    map_imperatively(Ledger, table, versioned=False)

    assert mapper_of(Ledger).version_id_col is None
    assert mapper_of(Tally).version_id_col is None


def test_entities_and_plain_classes_need_no_version() -> None:
    """Only aggregates are versioned; ``versioned`` is ignored by others."""

    class Line(Entity[int]):
        pass

    class Row:
        pass

    map_imperatively(Line, unversioned("framework_test_lines"))
    map_imperatively(Row, unversioned("framework_test_rows"), versioned=True)

    assert mapper_of(Line).version_id_col is None
    assert mapper_of(Row).version_id_col is None


def test_a_mapping_may_name_its_own_version_column() -> None:
    """``version_id_col`` is the mapping's choice, whatever the column is called."""

    class Legacy(AggregateRoot[int]):
        pass

    class Unlocked(AggregateRoot[int]):
        pass

    table = unversioned("framework_test_legacy")
    table.append_column(Column("row_version", Integer, nullable=False))
    map_imperatively(
        Legacy,
        table,
        version_id_col=table.c.row_version,
        properties={"version": table.c.row_version},
    )
    map_imperatively(
        Unlocked, unversioned("framework_test_unlocked"), version_id_col=None
    )

    assert mapper_of(Legacy).version_id_col is table.c.row_version
    assert mapper_of(Unlocked).version_id_col is None


def test_a_subclass_shares_its_base_aggregates_version() -> None:
    """Joined inheritance: the subclass's own table needs no version column."""

    class Base(AggregateRoot[int]):
        kind: str = "base"

    class Savings(Base):
        pass

    metadata = MetaData()
    bases = Table(
        "framework_test_bases",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("kind", String(10), nullable=False),
        Column("version", Integer, nullable=False),
    )
    savings = Table(
        "framework_test_savings",
        metadata,
        Column("id", Integer, ForeignKey(bases.c.id), primary_key=True),
    )
    map_imperatively(
        Base, bases, polymorphic_on=bases.c.kind, polymorphic_identity="base"
    )
    map_imperatively(Savings, savings, inherits=Base, polymorphic_identity="savings")

    assert mapper_of(Savings).version_id_col is bases.c.version


async def _open(id_: AccountId, *, versioned: bool) -> AccountId:
    """Store a balance of ``START`` under ``id_``."""
    async with BankUnitOfWork() as uow:
        if versioned:
            await uow.accounts.add(Account(id=id_, balance=START))
        else:
            await uow.tallies.add(Tally(id=id_, balance=START))
        await uow.commit()
    return id_


async def _balance(table: Table, id_: AccountId) -> Decimal:
    """The stored balance."""
    async with connections.engine().connect() as connection:
        balance = await connection.scalar(
            select(table.c.balance).where(table.c.id == id_)
        )
    assert isinstance(balance, Decimal)
    return balance


@pytest.mark.usefixtures("tables")
async def test_concurrent_withdrawals_from_a_hand_written_table_lose_nothing() -> None:
    """Every writer reads the same balance first; only one of them may commit.

    The rest fail with ``ConcurrentUpdate`` instead of writing a balance
    computed from a stale read (FIN-77's PoC lost 48 of 50 such updates).
    """
    id_ = await _open(AccountId(uuid4()), versioned=True)
    loaded = asyncio.Barrier(WRITERS)

    async def withdraw() -> str:
        try:
            async with BankUnitOfWork() as uow:
                account = await uow.accounts.get(id_)
                await loaded.wait()
                account.withdraw(AMOUNT)
                await uow.commit()
        except ConcurrentUpdate:
            return "concurrent_update"
        return "ok"

    outcomes = await asyncio.gather(*(withdraw() for _ in range(WRITERS)))

    assert sorted(outcomes) == ["concurrent_update"] * (WRITERS - 1) + ["ok"]
    assert await _balance(accounts, id_) == START - AMOUNT
    await connections.close_all()


@pytest.mark.usefixtures("tables")
async def test_an_unversioned_mapping_lets_the_last_writer_win() -> None:
    """What ``versioned=False`` gives up: a stale copy overwrites a newer one."""
    id_ = await _open(AccountId(uuid4()), versioned=False)
    async with BankUnitOfWork() as first, BankUnitOfWork() as second:
        mine, theirs = await first.tallies.get(id_), await second.tallies.get(id_)
        mine.balance -= AMOUNT
        theirs.balance -= AMOUNT
        await first.commit()
        await second.commit()

    assert await _balance(tallies, id_) == START - AMOUNT
    await connections.close_all()


async def _race(
    id_: AccountId, uow: BankUnitOfWork, *, then: Callable[[], Awaitable[object]]
) -> None:
    """Withdraw from ``id_`` in ``uow`` after another unit of work did; ``then``.

    The other one commits between this one's read and its write, so this
    one's write matches no row of its version, wherever it is flushed.
    """
    account = await uow.accounts.get(id_)
    async with BankUnitOfWork() as other:
        (await other.accounts.get(id_)).withdraw(AMOUNT)
        await other.commit()
    account.withdraw(AMOUNT)
    await then()


@pytest.mark.usefixtures("tables")
async def test_a_race_lost_at_a_flush_is_a_concurrent_update() -> None:
    """A stale write flushed in the block (a query, an explicit flush) is refused.

    As ``ConcurrentUpdate``, naming the model, never as SQLAlchemy's
    ``StaleDataError``, so ``Retry(retry_on=...)`` and the HTTP boundary
    see it.
    """
    id_ = await _open(AccountId(uuid4()), versioned=True)
    for flush in ("query", "flush"):
        with pytest.raises(ConcurrentUpdate) as caught:
            async with BankUnitOfWork() as uow:
                await _race(
                    id_,
                    uow,
                    then=uow.accounts.filter if flush == "query" else uow.session.flush,
                )
        assert caught.value.model == f"{Account.__module__}.Account"
        assert caught.value.code == "concurrent_update"
    assert await _balance(accounts, id_) == START - 2 * AMOUNT
    await connections.close_all()


@pytest.mark.usefixtures("tables")
async def test_a_race_lost_while_a_commit_is_staged_is_a_concurrent_update() -> None:
    """A domain's ``on_stage`` hook flushes the stale write: refused the same way."""
    flushed: list[str] = []

    async def stage(uow: object, events: object, tasks: object) -> None:
        assert isinstance(uow, SqlAlchemyUnitOfWork)
        flushed.append("stage")
        await uow.session.flush()

    registry.clear()
    with override_settings(INSTALLED_DOMAINS=[ALPHA]):
        setup()
        on_stage(stage, domain="alpha")
        id_ = await _open(AccountId(uuid4()), versioned=True)
        flushed.clear()
        with pytest.raises(ConcurrentUpdate) as caught:
            async with BankUnitOfWork() as uow:
                await _race(id_, uow, then=uow.commit)
        assert caught.value.model == f"{Account.__module__}.Account"
        assert flushed == ["stage", "stage"]  # The other's commit, then this one's.
    registry.clear()
    await connections.close_all()


@pytest.mark.usefixtures("tables")
async def test_run_retries_a_race_lost_at_a_flush() -> None:
    """``uow.run`` runs the work again, whole, and the second run commits."""
    id_ = await _open(AccountId(uuid4()), versioned=True)
    uow = BankUnitOfWork()
    runs = 0

    async def work() -> None:
        nonlocal runs
        runs += 1
        async with uow:
            if runs == 1:
                await _race(id_, uow, then=uow.session.flush)
            else:
                (await uow.accounts.get(id_)).withdraw(AMOUNT)
            await uow.commit()

    await uow.run(work, retry=Retry(attempts=2, retry_on=(ConcurrentUpdate,)))

    assert runs == 2
    assert await _balance(accounts, id_) == START - 2 * AMOUNT
    await connections.close_all()


@pytest.mark.usefixtures("tables")
async def test_a_race_lost_in_a_savepoint_is_a_concurrent_update() -> None:
    """The savepoint is undone, and the block sees ``ConcurrentUpdate``."""
    id_ = await _open(AccountId(uuid4()), versioned=True)
    async with BankUnitOfWork() as uow:
        account = await uow.accounts.get(id_)
        async with BankUnitOfWork() as other:
            (await other.accounts.get(id_)).withdraw(AMOUNT)
            await other.commit()
        with pytest.raises(ConcurrentUpdate) as caught:
            async with uow.savepoint():
                account.withdraw(AMOUNT)
    assert caught.value.model == f"{Account.__module__}.Account"
    assert await _balance(accounts, id_) == START - AMOUNT
    await connections.close_all()


def test_a_race_on_a_table_no_model_maps_names_no_model() -> None:
    """The model is named only where the ORM's message names a mapped table."""
    from sqlalchemy.orm.exc import StaleDataError

    from domainkit.db.uow import concurrent_update

    for message in (
        (
            "UPDATE statement on table 'framework_test_nowhere' expected to update "
            "1 row(s); 0 were matched."
        ),
        "Something else went stale.",
    ):
        error = concurrent_update(StaleDataError(message))
        assert (error.model, error.code) == (None, "concurrent_update")
    deleted = concurrent_update(
        StaleDataError(
            "DELETE statement on table 'framework_test_accounts' expected to "
            "delete 1 row(s); Only 0 were matched."
        )
    )
    assert deleted.model == f"{Account.__module__}.Account"
    assert str(deleted) == (
        "The data changed while this request was running; reload it and try again."
    )
