"""SEC-14b-2: the append-only check is satisfied by triggers that never fire.

``protection.missing`` (``accounting.E001`` at startup, and what
``protect`` restores after ``migrate``) only looks for an enabled trigger
of the right name and ``tgtype``. A trigger of that name with
``WHEN (false)``, or one that runs another function, passes the check, is
left alone by ``migrate``, and lets every ``UPDATE``/``DELETE`` through:
the docs promise a start refused when the triggers are "missing,
disabled, or changed". PostgreSQL only; fails on ``ac71361``.
"""

import os

import pytest
from sqlalchemy import text

from domainkit.contrib.accounting import AccountingUnitOfWork, post_entry, protection
from domainkit.db import connections
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_accounting import (  # noqa: F401 - the fixture.
    account,
    books,
    period,
    transfer,
    usd,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("books"),
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
    ),
]
TABLE: str = "domainkit_accounting_postings"
ROW: str = f"{TABLE}_append_only"


async def _rewritten(statement: str) -> list[str]:
    """Replace the postings' row trigger with ``statement``; what migrate leaves."""
    await period()
    cash, sales = await account("1000"), await account("4000")
    await post_entry(transfer(sales, cash, usd("100"), "s1"), AccountingUnitOfWork())
    async with connections.engine().begin() as connection:
        await connection.execute(
            text(f"DROP TRIGGER {ROW} ON domainkit_accounting_postings")
        )
        await connection.execute(text(statement))
    async with connections.engine().begin() as connection:
        await protection.protect(connection, "default")  # What `migrate` does.
    async with connections.engine().begin() as connection:
        changed = await connection.execute(
            text("UPDATE domainkit_accounting_postings SET amount = amount * 1000")
        )
    assert changed.rowcount == 2  # The books were rewritten, despite the triggers.
    return [str(problem) for problem in await protection.protection_problems()]


async def test_a_trigger_that_never_fires_is_reported() -> None:
    """``WHEN (false)``: same name, same type, never runs."""
    problems = await _rewritten(
        f"CREATE TRIGGER {ROW} BEFORE UPDATE OR DELETE ON {TABLE} "
        f"FOR EACH ROW WHEN (false) EXECUTE FUNCTION {ROW}()"
    )
    assert problems, "accounting.E001 must refuse a trigger that cannot fire"


async def test_a_trigger_running_another_function_is_reported() -> None:
    """Same name and type, but a function that lets the change through."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            text(
                "CREATE FUNCTION allow_it() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RETURN COALESCE(NEW, OLD); END $$"
            )
        )
    problems = await _rewritten(
        f"CREATE TRIGGER {ROW} BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION allow_it()"
    )
    assert problems, "accounting.E001 must refuse a trigger running another function"
