"""SEC-14a-2: the mapped money attribute compares each column on its own.

``map_imperatively`` maps a money pair to one attribute, a SQLAlchemy
``composite`` with its default comparator, which joins column comparisons
with ``AND``: ``Wallet.balance < Money.of("100", "USD")`` is ``balance <
100 AND balance_currency < 'USD'``. It finds 10.00 EUR, and never a USD
balance; ``>= 10 EUR`` finds 50.00 USD. docs/money.md:
"Money compares only with money of its currency, in memory and in SQL
alike".
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from domainkit.db import connections
from domainkit.money import Money
from domainkit_tests.test_money_db import (
    Wallet,
    WalletsUnitOfWork,
    database,  # noqa: F401 - a fixture.
    metadata,
    store,
    wallet,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def stored(database: str) -> AsyncIterator[None]:  # noqa: F811 - the fixture.
    """50.00 USD and 10.00 EUR, committed."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    await store(
        wallet("usd", Money.of("50", "USD")), wallet("eur", Money.of("10", "EUR"))
    )
    yield
    await connections.close_all()


async def names(condition: object) -> list[str]:
    """The names of the wallets ``select(Wallet).where(condition)`` finds."""
    async with WalletsUnitOfWork() as uow:
        found = await uow.session.scalars(select(Wallet).where(condition))  # type: ignore[arg-type]
        return sorted(each.name for each in found)


async def test_less_than_100_usd_is_the_usd_wallet(stored: None) -> None:
    """Under 100 USD: the 50 USD wallet, not the one in euros."""
    assert await names(Wallet.balance < Money.of("100", "USD")) == ["usd"]


async def test_at_least_10_eur_is_the_eur_wallet(stored: None) -> None:
    """At least 10 EUR: the wallet of 10 euros, not the one of 50 dollars."""
    assert await names(Wallet.balance >= Money.of("10", "EUR")) == ["eur"]
