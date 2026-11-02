"""SEC-16b-6: postings rewritten around the triggers still pass every check.

FIN-96 put what each entry moved (``amounts``) and its fingerprint
(``digest``) into ``EntryPosted``, and the guide now says "the audit
trail vouches for each entry ... A rewrite of postings and balances that
``verify_ledger`` cannot see ... no longer matches the trail". But
nothing compares them: ``verify_ledger`` (``manage.py accounting
verify``) still checks only that entries balance and balances add up,
``audit verify`` checks the trail's own chain, and no command reads
``EntryPosted`` back against the postings (the audit's "Do" for FIN-96:
"have ``accounting verify`` check entries against the trail"). Whoever
owns the schema disables the append-only triggers, turns a 5,000.00
payout into 50.00 on both of its postings and both balances, enables
them again: the books "balance", the trail verifies, and the 4,950.00
is gone from the record that every report reads.

The test asserts the safe behaviour (``accounting verify`` reports the
entry) and fails on ``675dfd6``, on SQLite (no triggers) and PostgreSQL.
"""

import io
from collections.abc import Iterator
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import select, text, update

from domainkit.contrib.accounting import AccountingUnitOfWork, post_entry
from domainkit.contrib.accounting.tables import accounts, postings
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.management import CommandError, call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import ALPHA, Migrated, Shape
from domainkit_tests.test_accounting import account, period, transfer, usd

AUDITED: Final = Shape(
    "accounting_audited",
    "accounting",
    {
        "INSTALLED_DOMAINS": [
            "domainkit.contrib.audit",
            "domainkit.contrib.accounting",
            ALPHA,
        ]
    },
)
TABLES: Final = ("domainkit_accounting_entries", "domainkit_accounting_postings")


@pytest.fixture
def project(migrated: Migrated) -> Iterator[str]:
    """The ledger and the audit trail, migrated."""
    with migrated(AUDITED) as url:
        yield url


def test_a_payout_rewritten_on_both_sides_is_found_by_verify(project: str) -> None:
    """5,000.00 becomes 50.00 in both postings and both balances, triggers off."""

    async def make() -> tuple[object, object, object]:
        await period()
        bank, wallet = await account("1000"), await account("2000")
        entry = await post_entry(
            transfer(bank, wallet, usd("5000"), "payout-1"), AccountingUnitOfWork()
        )
        return bank, wallet, entry

    bank, wallet, entry = run_async(make())

    async def rewrite() -> None:
        postgres = project.startswith("postgresql")
        async with connections.engine().begin() as connection:
            if postgres:  # The schema owner turns the append-only triggers off.
                for table in TABLES:
                    await connection.execute(
                        text(f"ALTER TABLE {table} DISABLE TRIGGER USER")
                    )
            await connection.execute(
                update(postings)
                .where(postings.c.entry_id == entry.id)  # type: ignore[attr-defined]
                .values(amount=Decimal("50"))
            )
            for each in (bank, wallet):
                current = await connection.scalar(
                    select(accounts.c.balance).where(accounts.c.id == each.id)  # type: ignore[attr-defined]
                )
                change = Decimal("4950") if current < 0 else Decimal("-4950")  # type: ignore[operator]
                await connection.execute(
                    update(accounts)
                    .where(accounts.c.id == each.id)  # type: ignore[attr-defined]
                    .values(balance=current + change)  # type: ignore[operator]
                )
            if postgres:
                for table in TABLES:
                    await connection.execute(
                        text(f"ALTER TABLE {table} ENABLE TRIGGER USER")
                    )

    run_async(rewrite())

    async def vouched() -> list[object]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(audit_log.c.details).where(
                    audit_log.c.action == "accounting.entry_posted"
                )
            )
            return [row.details for row in rows]

    # The trail still says 5,000.00: the evidence is there, unread.
    assert "5000" in str(run_async(vouched()))
    with pytest.raises(CommandError):
        call_command("accounting", "verify", "--operator", "t", stdout=io.StringIO())
