"""FIN-90: a statement imported, or an account reconciled, over HTTP is never audited.

``manage.py accounting import`` and ``reconcile`` record themselves in the
audit trail (``operator.accounting.import``, ``.reconcile``), as every
command that changes data must (ADR 0070). The same two acts over HTTP,
``POST /accounts/{id}/statements`` and ``/reconcile``, write the lines,
the matches, and the breaks straight through a connection: no domain
event, no audit row, no actor. A clerk with ``reconcile_account`` can
import a fabricated bank statement whose lines "match" postings the bank
never saw, clearing the breaks that would have shown them, and the trail
the threat model promises ("every change is an event the trail records
with its actor") holds nothing of it.
"""

from collections.abc import Iterator
from typing import Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.db import connections
from domainkit.http import get_asgi_application
from domainkit.management.base import run_async
from domainkit_tests.conftest import Migrated, Shape
from domainkit_tests.test_accounting_api import (
    ACCOUNTING_API,
    API,
    EVERY,
    open_books,
    sale,
    user,
)

AUDITED: Final = Shape(
    "accounting_api_audited",
    "accounting",
    {
        **ACCOUNTING_API.settings,
        "INSTALLED_DOMAINS": [
            *ACCOUNTING_API.settings["INSTALLED_DOMAINS"],
            "domainkit.contrib.audit",
        ],
    },
)


@pytest.fixture
def audited(migrated: Migrated) -> Iterator[TestClient]:
    """The ledger's API, with the audit trail, in a migrated project."""
    with migrated(AUDITED), TestClient(get_asgi_application()) as client:
        yield client


def trail() -> list[tuple[str, str]]:
    """``(action, actor)`` of every audit row, oldest first."""

    async def read() -> list[tuple[str, str]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(audit_log.c.action, audit_log.c.actor).order_by(
                    audit_log.c.occurred_at, audit_log.c.id
                )
            )
            return [(row.action, row.actor) for row in rows]

    return run_async(read())


def test_importing_and_reconciling_over_http_are_in_the_trail(
    audited: TestClient,
) -> None:
    """Both acts leave a row naming the clerk, as their commands do."""
    client = audited
    clerk = user("clerk", *EVERY)
    clerk_id = str(_id("clerk"))
    books = open_books(client, clerk)
    posted = client.post(
        f"{API}/entries", json=sale(books, "s-1", "10.50"), headers=clerk
    )
    assert posted.status_code == 201, posted.json()
    account = books["cash"]["id"]
    before = trail()
    assert ("accounting.entry_posted", clerk_id) in before  # The trail works.

    imported = client.post(
        f"{API}/accounts/{account}/statements",
        content="id,date,amount,currency,reference\nb-1,2026-10-15,10.50,USD,s-1\n",
        headers={**clerk, "Content-Type": "text/csv"},
    )
    reconciled = client.post(f"{API}/accounts/{account}/reconcile", headers=clerk)
    assert (imported.status_code, reconciled.status_code) == (200, 200)
    assert reconciled.json()["matched"] == {"reference": 1}

    added = trail()[len(before) :]
    assert len(added) >= 2, added
    assert all(actor == clerk_id for _, actor in added), added


def _id(username: str) -> object:
    """The id of the user ``username``."""

    async def find() -> object:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(username)
            assert found is not None
            return found.id

    return run_async(find())
