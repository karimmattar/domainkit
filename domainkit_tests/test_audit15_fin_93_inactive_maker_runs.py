"""FIN-93: an approved command runs as a maker who was deactivated meanwhile.

An approved request runs "as its maker" (``execute_request``), but only
``grant_superuser`` checks, when it runs, that the maker still may
(SEC-15b-1). Every other guarded command, the ledger's large postings
and captures and the fintech profile's large transfers among them, runs
for a maker deactivated (a clerk dismissed, a customer frozen for fraud)
or stripped of the maker's permission after submitting: the approvers,
deciding a request days later (``EXPIRES_AFTER`` is 3 days), see neither.
Freezing a person must stop the instructions they left waiting.
"""

from datetime import date
from typing import Any

from fastapi.testclient import TestClient

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    OpenAccount,
    OpenPeriod,
    open_account,
    open_period,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.test_approvals_api import (  # noqa: F401 - the fixture.
    API,
    CLERK,
    client,
    decide,
    submit,
    user,
)


async def _books() -> tuple[str, str]:
    """October, and a cash and a sales account; their ids."""
    await open_period(
        OpenPeriod(
            name="2026-10", starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31)
        ),
        AccountingUnitOfWork(),
    )
    ids = []
    for code, kind in (("1000", AccountType.ASSET), ("4000", AccountType.INCOME)):
        opened = await open_account(
            OpenAccount(code=code, name=code, type=kind, currency="USD"),
            AccountingUnitOfWork(),
        )
        ids.append(str(opened.id))
    return ids[0], ids[1]


async def _deactivate(username: str) -> None:
    """Deactivate ``username``, as an administrator would."""
    async with AuthUnitOfWork() as uow:
        found = await uow.users.get_by_natural_key(username)
        assert found is not None
        found.set_active(False)
        await uow.commit()


async def _booked(reference: str) -> int:
    """Entries posted under ``reference``."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            entries.select().where(entries.c.reference == reference)
        )
        return len(rows.all())


def test_a_request_of_a_maker_deactivated_since_never_runs(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Two approvals after the maker was frozen: the 5,000 entry is not posted."""
    cash, sales = run_async(_books())
    maker = user("maker", *CLERK)
    ann = user("ann", *CLERK)
    bob = user("bob", *CLERK)
    amount = {"amount": "5000.00", "currency": "USD"}
    arguments: dict[str, Any] = {
        "source": "shop",
        "reference": "big",
        "booked_on": "2026-10-15",
        "lines": [
            {"account_id": cash, "side": "debit", "amount": amount},
            {"account_id": sales, "side": "credit", "amount": amount},
        ],
    }
    submitted = submit(client, maker, "accounting.post_entry", arguments)
    assert submitted.status_code == 201, submitted.json()
    request = submitted.json()

    run_async(_deactivate("maker"))
    assert decide(client, request, ann).status_code == 200
    assert decide(client, request, bob).json()["status"] == "approved"
    run_async(drain())

    done = client.get(f"{API}/{request['id']}", headers=ann).json()
    assert done["status"] == "failed", done
    assert run_async(_booked("big")) == 0


async def _revoke(username: str, key: str) -> None:
    """Take ``key`` back from ``username``."""
    async with AuthUnitOfWork() as uow:
        found = await uow.users.get_by_natural_key(username)
        assert found is not None
        found.revoke(*await uow.permissions.get_many([key]))
        await uow.commit()


def test_a_request_of_a_maker_who_lost_the_permission_never_runs(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Still active, but no longer allowed to post: the request fails."""
    cash, sales = run_async(_books())
    maker = user("maker", *CLERK)
    ann = user("ann", *CLERK)
    bob = user("bob", *CLERK)
    amount = {"amount": "5000.00", "currency": "USD"}
    request = submit(
        client,
        maker,
        "accounting.post_entry",
        {
            "source": "shop",
            "reference": "revoked",
            "booked_on": "2026-10-15",
            "lines": [
                {"account_id": cash, "side": "debit", "amount": amount},
                {"account_id": sales, "side": "credit", "amount": amount},
            ],
        },
    ).json()

    run_async(_revoke("maker", "accounting.add_entry"))
    decide(client, request, ann)
    decide(client, request, bob)
    run_async(drain())

    done = client.get(f"{API}/{request['id']}", headers=ann).json()
    assert (done["status"], done["reason"]) == ("failed", "maker_not_allowed")
    assert run_async(_booked("revoked")) == 0
