"""What the ledger holds about a person: the accounts kept for them (``holder``).

``privacy export`` lists a subject's accounts, their balances, and their
postings. ``privacy erase`` keeps the books, which the law asks a business
to keep for years (and which are append-only besides), and pseudonymises
them: each of the subject's accounts names one ``erased:`` holder instead,
the same for all of them, so the books still show which accounts were
one person's, and nothing names who (FIN-94). While the subject holds an
open account with money on it, or held, the erasure is held
(``hold_erasure``): a business does not erase a person it still owes, or
who still owes it; close the accounts first. The erasure checks again
with the accounts locked, so money that landed after the check holds it
then, and it closes the subject's open accounts, all empty, so none
takes money for an erased holder (SEC-16b-4). Entries' descriptions and
memos, and statement lines, must not name people: they are kept as
written.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import exists, or_, select, update

from domainkit.db import connections, dbclock

from .models import ZERO, AccountStatus, amount_text
from .tables import accounts, entries, postings
from .uow import accounting_alias

ERASED: Final = "erased:"
"""The start of the holder that replaces an erased subject on their accounts."""
FUNDED: Final = "Holds an open account with money on it or held: close it first."
"""Why the ledger holds an erasure."""


async def export_subject(subject: str) -> Mapping[str, object]:
    """The subject's accounts, each with its postings, oldest first."""
    async with connections.engine(accounting_alias()).connect() as connection:
        held = (
            await connection.execute(
                select(accounts)
                .where(accounts.c.holder == subject)
                .order_by(accounts.c.opened_at, accounts.c.id)
            )
        ).all()
        found: list[dict[str, Any]] = []
        for account in held:
            code = account.currency
            lines = await connection.execute(
                select(
                    postings.c.entry_id,
                    postings.c.booked_on,
                    postings.c.side,
                    postings.c.amount,
                    postings.c.memo,
                    entries.c.reference,
                    entries.c.description,
                )
                .join(entries, entries.c.id == postings.c.entry_id)
                .where(postings.c.account_id == account.id)
                .order_by(postings.c.booked_on, entries.c.posted_at, postings.c.line)
            )
            found.append(
                {
                    "id": str(account.id),
                    "code": account.code,
                    "name": account.name,
                    "currency": code,
                    "status": account.status.value,
                    "balance": amount_text(account.balance, code),
                    "held": amount_text(account.held, code),
                    "opened_at": account.opened_at.isoformat(),
                    "postings": [
                        {
                            "entry_id": str(line.entry_id),
                            "booked_on": line.booked_on.isoformat(),
                            "side": line.side.value,
                            "amount": amount_text(line.amount, code),
                            "reference": line.reference,
                            "description": line.description,
                            "memo": line.memo,
                        }
                        for line in lines
                    ],
                }
            )
    return {"accounts": found} if found else {}


def _funded(holder: str) -> Any:  # noqa: ANN401 - a select.
    """Whether ``holder`` has an open account with money on it, or held."""
    return select(
        exists().where(
            accounts.c.holder == holder,
            accounts.c.status == AccountStatus.OPEN,
            or_(accounts.c.balance != ZERO, accounts.c.held != ZERO),
        )
    )


async def hold_erasure(subject: str) -> str | None:
    """Why the subject's books must stay theirs for now: an account with money on it."""
    async with connections.engine(accounting_alias()).connect() as connection:
        funded = await connection.scalar(_funded(subject))
    return FUNDED if funded else None


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Name one pseudonym as the holder of the subject's accounts, closing them.

    One transaction: renaming the accounts locks them (posting waits, or
    is waited for), then they are checked again, and the open ones,
    empty, are closed on the database's clock. Returns how many accounts
    it renamed.

    Raises:
        ErasureHeld: Money landed on, or was held on, an open account since
            ``hold_erasure`` checked (SEC-16b-4); nothing here is erased.
    """
    pseudonym = f"{ERASED}{uuid.uuid4().hex}"
    async with connections.engine(accounting_alias()).begin() as connection:
        result = await connection.execute(
            update(accounts)
            .where(accounts.c.holder == subject)
            .values(holder=pseudonym, version=accounts.c.version + 1)
        )
        if not result.rowcount:
            return {}
        if await connection.scalar(_funded(pseudonym)):
            from domainkit.contrib.privacy import ErasureHeld

            raise ErasureHeld(
                "This person's data must be kept for now: nothing was erased.",
                held={"accounting": FUNDED},
            )
        await connection.execute(
            update(accounts)
            .where(
                accounts.c.holder == pseudonym, accounts.c.status == AccountStatus.OPEN
            )
            .values(
                status=AccountStatus.CLOSED,
                closed_at=dbclock.now(connection.dialect.name),
            )
        )
    return {"accounts": result.rowcount}
