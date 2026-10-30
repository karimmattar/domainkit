"""What the ledger holds about a person: the accounts kept for them (``holder``).

``privacy export`` lists a subject's accounts, their balances, and their
postings. ``privacy erase`` keeps the books, which the law asks a business
to keep, and are append-only besides: it unlinks the subject from their
accounts instead, so nothing in the ledger names them any more. Entries'
descriptions and memos, and statement lines, must not name people: they
are kept as written.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update

from domainkit.db import connections

from .models import amount_text
from .tables import accounts, entries, postings
from .uow import accounting_alias


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


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Unlink the subject from their accounts; how many there were."""
    async with connections.engine(accounting_alias()).begin() as connection:
        result = await connection.execute(
            update(accounts)
            .where(accounts.c.holder == subject)
            .values(holder=None, version=accounts.c.version + 1)
        )
    return {"accounts": result.rowcount} if result.rowcount else {}
