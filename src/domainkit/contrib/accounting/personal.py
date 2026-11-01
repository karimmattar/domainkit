"""What the ledger holds about a person: the accounts kept for them (``holder``).

``privacy export`` lists a subject's accounts, their balances, and their
postings. ``privacy erase`` keeps the books, which the law asks a business
to keep for years (and which are append-only besides), and pseudonymises
them: each of the subject's accounts names one ``erased:`` holder instead,
the same for all of them, so the books still show which accounts were
one person's, and nothing names who (FIN-94). While the subject holds an
open account with money on it, or held, the erasure is held
(``hold_erasure``): a business does not erase a person it still owes, or
who still owes it; close the accounts first. Entries' descriptions and
memos, and statement lines, must not name people: they are kept as
written.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import or_, select, update

from domainkit.db import connections

from .models import ZERO, AccountStatus, amount_text
from .tables import accounts, entries, postings
from .uow import accounting_alias

ERASED: Final = "erased:"
"""The start of the holder that replaces an erased subject on their accounts."""


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


async def hold_erasure(subject: str) -> str | None:
    """Why the subject's books must stay theirs for now: an account with money on it."""
    async with connections.engine(accounting_alias()).connect() as connection:
        funded = await connection.scalar(
            select(accounts.c.id)
            .where(
                accounts.c.holder == subject,
                accounts.c.status == AccountStatus.OPEN,
                or_(accounts.c.balance != ZERO, accounts.c.held != ZERO),
            )
            .limit(1)
        )
    if funded is None:
        return None
    return "Holds an open account with money on it or held: close it first."


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Name one pseudonym as the holder of the subject's accounts; how many."""
    pseudonym = f"{ERASED}{uuid.uuid4().hex}"
    async with connections.engine(accounting_alias()).begin() as connection:
        result = await connection.execute(
            update(accounts)
            .where(accounts.c.holder == subject)
            .values(holder=pseudonym, version=accounts.c.version + 1)
        )
    return {"accounts": result.rowcount} if result.rowcount else {}
