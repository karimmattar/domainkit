"""``verify_ledger`` compares the books with the trail's ``EntryPosted`` (SEC-16b-6)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import insert, text, update

from domainkit.contrib.accounting import AccountingUnitOfWork, post_entry
from domainkit.contrib.accounting.reports import verify_ledger
from domainkit.contrib.accounting.tables import entries, postings
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.conftest import Migrated
from domainkit_tests.test_accounting import account, period, transfer, usd
from domainkit_tests.test_sec16b_6_rewritten_postings_pass_verify import AUDITED

TABLES: Final = ("domainkit_accounting_entries", "domainkit_accounting_postings")
GONE: Final = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def project(migrated: Migrated) -> Iterator[str]:
    """The ledger and the audit trail, migrated."""
    with migrated(AUDITED) as url:
        yield url


def _trail_row(details: dict[str, object]) -> dict[str, object]:
    """An ``EntryPosted`` row of the trail with ``details``."""
    return {
        "id": uuid.uuid4(),
        "occurred_at": datetime(2026, 10, 30, tzinfo=UTC),
        "actor": "system",
        "action": "accounting.entry_posted",
        "outcome": "success",
        "target": None,
        "request_id": None,
        "client_ip": None,
        "event_id": uuid.uuid4(),
        "details": details,
    }


def test_each_way_an_entry_differs_from_the_trail_is_named(project: str) -> None:
    """Renamed, posted to another account too, re-described, gone, or legacy."""

    async def make() -> list[object]:
        await period()
        bank, wallet, other = [await account(code) for code in ("1", "2", "3")]
        return [
            bank,
            wallet,
            other,
            *[
                await post_entry(
                    transfer(bank, wallet, usd("10"), reference),
                    AccountingUnitOfWork(),
                )
                for reference in ("same", "renamed", "extra", "described", "inexact")
            ],
        ]

    _, _, other, same, renamed, extra, described, inexact = run_async(make())

    async def tamper() -> None:
        postgres = project.startswith("postgresql")
        async with connections.engine().begin() as connection:
            if postgres:
                for table in TABLES:
                    await connection.execute(
                        text(f"ALTER TABLE {table} DISABLE TRIGGER USER")
                    )
            await connection.execute(
                update(entries)
                .where(entries.c.id == renamed.id)  # type: ignore[attr-defined]
                .values(reference="other")
            )
            await connection.execute(
                update(entries)
                .where(entries.c.id == described.id)  # type: ignore[attr-defined]
                .values(description="changed")
            )
            await connection.execute(
                update(postings)
                .where(
                    postings.c.entry_id == inexact.id,  # type: ignore[attr-defined]
                    postings.c.side == "credit",
                )
                .values(amount=Decimal("10.0001"))  # Past the dollar's cents.
            )
            await connection.execute(
                insert(postings),
                [
                    {
                        "id": uuid.uuid4(),
                        "entry_id": extra.id,  # type: ignore[attr-defined]
                        "line": 3 + n,
                        "account_id": other.id,  # type: ignore[attr-defined]
                        "side": side,
                        "amount": Decimal(5),
                        "currency": "USD",
                        "booked_on": extra.booked_on,  # type: ignore[attr-defined]
                        "memo": "",
                    }
                    for n, side in enumerate(("debit", "credit"))
                ],
            )
            if postgres:
                for table in TABLES:
                    await connection.execute(
                        text(f"ALTER TABLE {table} ENABLE TRIGGER USER")
                    )
            await connection.execute(
                insert(audit_log),
                [
                    _trail_row(
                        {
                            "entry_id": str(GONE),
                            "source": "tests",
                            "reference": "gone",
                            "amounts": [],
                            "digest": "0" * 64,
                        }
                    ),
                    _trail_row({"entry_id": str(uuid.uuid4()), "source": "old"}),
                    _trail_row({"entry_id": str(uuid.uuid4())} | {"digest": None})
                    | {"action": "accounting.account_opened"},
                ],
            )

    run_async(tamper())

    found = {
        (problem.id, problem.detail)
        for problem in run_async(verify_ledger())
        if problem.kind == "entry_differs"
    }
    generic = "its date, description, memos, source, or reference differ from the trail"
    assert found == {
        (str(GONE), "in the audit trail, not in the books"),
        (str(renamed.id), generic),  # type: ignore[attr-defined]
        (str(described.id), generic),  # type: ignore[attr-defined]
        (str(extra.id), f"{other.id}: posted to, and not in the trail"),  # type: ignore[attr-defined]
        (
            str(inexact.id),  # type: ignore[attr-defined]
            (
                f"{inexact.postings[0].account_id}: debits 0.0000, "  # type: ignore[attr-defined]
                "credits 10.0001; the trail: debits 0.0000, credits 10.0000"
            ),
        ),
    }
    assert str(same.id) not in {each for each, _ in found}  # type: ignore[attr-defined]
