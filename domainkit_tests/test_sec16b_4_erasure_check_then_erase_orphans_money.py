"""SEC-16b-4: money that lands between the erasure's check and its erase is orphaned.

FIN-94 added a refusal phase: ``erase_subject`` asks every domain's
``on_retain_subject`` first, and the ledger's (``hold_erasure``) holds
the erasure while the person has an open account with money on it or
held. But the check reads in a transaction of its own and takes no lock;
the ledger's ``on_erase_subject`` then renames the holder of *every*
account of the subject (``UPDATE accounts SET holder = 'erased:...'``),
funded or not, without checking again. Between the two, the other
domains' checks run, then the audit trail, keys, messaging, and every
``Personal`` model are erased first: a top-up, an incoming transfer, a
hold placed by a card authorisation lands in that window, and the
account ends open, with money on it, owed to no one the books can name.
That is FIN-94's very failure ("a funded account is orphaned"), now a
race rather than the rule.

The window is opened here by another domain's ``on_retain_subject`` that
lets the erasure go on after a deposit commits, standing in for a
concurrent request (the check-then-act gap is the same). The test
asserts the safe behaviour (the erasure is held, or no open account of
an erased holder has money on it) and fails on ``675dfd6``.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import select

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import on_retain_subject
from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    OpenAccount,
    OpenPeriod,
    PostEntry,
    open_account,
    open_period,
    post_entry,
)
from domainkit.contrib.accounting.tables import accounts
from domainkit.contrib.privacy import ErasureHeld, erase_subject
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit.money import Money

AUDIT: Final = "domainkit.contrib.audit"
PRIVACY: Final = "domainkit.contrib.privacy"
ACCOUNTING: Final = "domainkit.contrib.accounting"
ALPHA: Final = "domainkit_tests.sample_domains.alpha"
CUSTOMER: Final = "customer-1"


@pytest.fixture
def bank(database_url: str, tmp_path: Path) -> Iterator[str]:
    """The ledger, privacy, and the audit trail, migrated."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, PRIVACY, ACCOUNTING, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "bank")
        call_command("migrate")
        yield database_url
        run_async(connections.close_all())
    registry.clear()


def usd(amount: str) -> Money:
    """``amount`` dollars."""
    return Money.of(amount, "USD")


async def books() -> tuple[object, object]:
    """October, a bank account, and the customer's empty wallet."""
    await open_period(
        OpenPeriod(
            name="2026-10", starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31)
        ),
        AccountingUnitOfWork(),
    )
    bank = await open_account(
        OpenAccount(
            code="1000",
            name="Bank",
            type=AccountType.ASSET,
            currency="USD",
            may_go_negative=True,
        ),
        AccountingUnitOfWork(),
    )
    wallet = await open_account(
        OpenAccount(
            code="2000",
            name="Wallet",
            type=AccountType.LIABILITY,
            currency="USD",
            holder=CUSTOMER,
        ),
        AccountingUnitOfWork(),
    )
    return bank, wallet


def test_money_landing_during_an_erasure_holds_it_or_keeps_its_holder(
    bank: str,
) -> None:
    """The wallet is empty at the check; a top-up commits; the erasure goes on."""
    cash, wallet = run_async(books())

    async def top_up_meanwhile(subject: str, reason: str) -> str | None:
        # After the ledger's own check said "nothing owed" (domain order).
        await post_entry(
            PostEntry(
                source="topups",
                reference="t-1",
                booked_on=date(2026, 10, 15),
                lines=(
                    Line.debit(cash.id, usd("250")),  # type: ignore[attr-defined]
                    Line.credit(wallet.id, usd("250")),  # type: ignore[attr-defined]
                ),
            ),
            AccountingUnitOfWork(),
        )
        return None

    on_retain_subject(top_up_meanwhile, domain="alpha")

    async def erase() -> bool:
        try:
            await erase_subject(CUSTOMER, reason="ticket 1")
        except ErasureHeld:
            return True
        return False

    held = run_async(erase())

    async def orphaned() -> list[tuple[str, object]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(accounts.c.holder, accounts.c.balance).where(
                    accounts.c.holder.like("erased:%"), accounts.c.balance != 0
                )
            )
            return [(row.holder, row.balance) for row in rows]

    assert held or run_async(orphaned()) == []
