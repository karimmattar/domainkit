"""SEC-14b-5: erasure left the subject in the account code the docs recommended.

``docs/contrib/accounting.md`` opened a wallet as
``OpenAccount(code=f"2100-{user.id}", ..., holder=str(user.id))``, and
``docs/privacy.md`` promised that once ``holder`` is cleared "nothing in
the ledger names the subject". ``erase_subject`` clears ``holder`` only:
the code (unique, shown by ``GET /accounts``, every report, and the audit
trail's ``AccountOpened``) still named them. (With domainkit's own UUID
user ids the example was refused outright: 41 characters for a
32-character code.) The example now takes an opaque code, with the person
in ``holder`` alone, and privacy.md says what erasure leaves.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import or_, select

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    OpenAccount,
    open_account,
)
from domainkit.contrib.accounting.models import CODE
from domainkit.contrib.accounting.personal import erase_subject
from domainkit.contrib.accounting.tables import accounts
from domainkit.db import connections
from domainkit_tests.test_accounting import books  # noqa: F401 - the fixture.

DOCS = Path(__file__).resolve().parents[1] / "docs"
GUIDE = DOCS / "contrib" / "accounting.md"
CODES = re.compile(r"^\s*code=(.+?),", re.MULTILINE)


def test_the_documented_codes_name_no_person() -> None:
    """Each code the guide opens an account with is opaque, and fits ``CODE``."""
    codes = CODES.findall(GUIDE.read_text("utf-8"))
    assert 'f"2100-{wallet_number:06d}"' in codes
    for code in codes:
        assert "user" not in code, code
        assert "holder" not in code, code
    assert CODE.fullmatch(f"2100-{999_999:06d}")


def test_privacy_says_what_erasure_keeps() -> None:
    """The register's promise is no wider than what ``erase_subject`` does."""
    text = (DOCS / "privacy.md").read_text("utf-8")
    assert "nothing in the ledger names the subject" not in text
    assert "Account codes and names, references, descriptions, memos" in text


@pytest.mark.anyio
@pytest.mark.usefixtures("books")
async def test_nothing_names_an_erased_holder() -> None:
    """Opened as the docs do, no account row carries the subject's id once erased."""
    user_id, wallet_number = "4711", 42
    await open_account(
        OpenAccount(
            code=f"2100-{wallet_number:06d}",
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
