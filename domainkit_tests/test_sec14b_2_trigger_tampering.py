"""SEC-14b-2: the append-only check is satisfied by triggers that never fire.

``protection.missing`` (``accounting.E001`` at startup, and what
``protect`` restored after ``migrate``) looked only for an enabled trigger
of the right name and ``tgtype``. A trigger of that name with
``WHEN (false)``, one that runs another function, or the right function
with another body passed the check, was left alone by ``migrate``, and let
every ``UPDATE``/``DELETE`` through: the docs promise a start refused when
the triggers are "missing, disabled, or changed". Each is now reported,
and ``protect`` (``migrate``) makes the triggers afresh. PostgreSQL only;
failed on ``ac71361``.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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
REWRITE: str = f"UPDATE {TABLE} SET amount = amount * 1000"  # noqa: S608 - constants.
ALLOW: str = "LANGUAGE plpgsql AS $$ BEGIN RETURN COALESCE(NEW, OLD); END $$"


async def _tampered(*statements: str) -> None:
    """Post an entry, then change the postings' row protection with ``statements``.

    The change must open the books (the ``UPDATE`` goes through) and be
    reported; ``protect``, what ``migrate`` runs, must close them again.
    """
    await period()
    cash, sales = await account("1000"), await account("4000")
    await post_entry(transfer(sales, cash, usd("100"), "s1"), AccountingUnitOfWork())
    assert await protection.protection_problems() == []
    async with connections.engine().begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))
    async with connections.engine().connect() as connection:
        opened = await connection.execute(text(REWRITE))
        assert opened.rowcount == 2  # The tampering is real: the books could change.
        await connection.rollback()

    problems = [str(problem) for problem in await protection.protection_problems()]
    assert len(problems) == 1
    assert ROW in problems[0]
    assert problems[0].startswith("Entries and postings in 'default' can be changed")

    async with connections.engine().begin() as connection:
        await protection.protect(connection, "default")  # What `migrate` does.
    assert await protection.protection_problems() == []
    with pytest.raises(DBAPIError, match=f"{TABLE} is append-only"):
        async with connections.engine().begin() as connection:
            await connection.execute(text(REWRITE))


async def test_a_trigger_that_never_fires_is_reported() -> None:
    """``WHEN (false)``: same name, same type, never runs."""
    await _tampered(
        f"DROP TRIGGER {ROW} ON {TABLE}",
        f"CREATE TRIGGER {ROW} BEFORE UPDATE OR DELETE ON {TABLE} "
        f"FOR EACH ROW WHEN (false) EXECUTE FUNCTION {ROW}()",
    )


async def test_a_trigger_on_some_columns_only_is_reported() -> None:
    """``UPDATE OF memo``: same name, same type, but the amount is not watched."""
    await _tampered(
        f"DROP TRIGGER {ROW} ON {TABLE}",
        f"CREATE TRIGGER {ROW} BEFORE UPDATE OF memo OR DELETE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {ROW}()",
    )


async def test_a_trigger_running_another_function_is_reported() -> None:
    """Same name and type, but a function that lets the change through."""
    try:
        await _tampered(
            f"CREATE OR REPLACE FUNCTION allow_it() RETURNS trigger {ALLOW}",
            f"DROP TRIGGER {ROW} ON {TABLE}",
            f"CREATE TRIGGER {ROW} BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION allow_it()",
        )
    finally:
        async with connections.engine().begin() as connection:
            await connection.execute(text("DROP FUNCTION IF EXISTS allow_it() CASCADE"))


async def test_the_function_with_another_body_is_reported() -> None:
    """The trigger untouched, its function replaced in place."""
    await _tampered(f"CREATE OR REPLACE FUNCTION {ROW}() RETURNS trigger {ALLOW}")
