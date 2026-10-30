"""SEC-14b-5: erasure leaves the subject in the account code the docs recommend.

``docs/contrib/accounting.md`` opens a wallet as
``OpenAccount(code=f"2100-{user.id}", ..., holder=str(user.id))``, and
``docs/privacy.md`` promises that once ``holder`` is cleared "nothing in
the ledger names the subject". ``erase_subject`` clears ``holder`` only:
the code (unique, shown by ``GET /accounts``, every report, and the
audit trail's ``AccountOpened``) still names them. (With domainkit's own
UUID user ids the example is refused outright: 41 characters for a
32-character code.) Fails on ``ac71361``.
"""

import pytest
from sqlalchemy import or_, select

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    OpenAccount,
    open_account,
)
from domainkit.contrib.accounting.personal import erase_subject
from domainkit.contrib.accounting.tables import accounts
from domainkit.db import connections
from domainkit_tests.test_accounting import books  # noqa: F401 - the fixture.

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]


async def test_nothing_names_an_erased_holder() -> None:
    """After ``privacy erase``, no account row carries the subject's id."""
    user_id = "4711"  # The docs' example, with an id that fits the code.
    await open_account(
        OpenAccount(
            code=f"2100-{user_id}",
            name="Wallet",
            type=AccountType.LIABILITY,
            currency="EGP",
            holder=user_id,
            may_go_negative=False,
        ),
        AccountingUnitOfWork(),
    )

    assert await erase_subject(user_id) == {"accounts": 1}

    async with connections.engine().connect() as connection:
        naming = (
            await connection.execute(
                select(accounts.c.code, accounts.c.holder).where(
                    or_(
                        accounts.c.holder == user_id,
                        accounts.c.code.contains(user_id),
                        accounts.c.name.contains(user_id),
                    )
                )
            )
        ).all()
    assert naming == [], "the ledger still names the erased subject"
