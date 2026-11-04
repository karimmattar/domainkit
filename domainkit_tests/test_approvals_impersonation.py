"""Requests are made and decided in one's own name: never under impersonation.

Makers and approvers are counted as people, by the principal's id. Staff
acting as a user count as that user, so the audit found one staff member
making a request as one user and approving it as themselves and as
another (A3-1).
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Final

import pytest
from fastapi.testclient import TestClient

from domainkit.contrib.auth import AbstractUser, AuthUnitOfWork
from domainkit.http import get_asgi_application
from domainkit.management.base import run_async
from domainkit_tests.conftest import Migrated, Shape
from domainkit_tests.test_accounting_api import open_books, sale
from domainkit_tests.test_approvals_api import (
    APPROVALS_API,
    CLERK,
    _id,
    decide,
    submit,
    user,
)
from domainkit_tests.test_auth_http import bearer

IMPERSONATING: Final = Shape(
    "approvals_impersonation",
    APPROVALS_API.message,
    {
        **APPROVALS_API.settings,
        "AUTH": {
            **APPROVALS_API.settings["AUTH"],
            "IMPERSONATION_TTL": timedelta(minutes=30),
        },
    },
)


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The approvals API, with impersonation on."""
    with migrated(IMPERSONATING), TestClient(get_asgi_application()) as test_client:
        yield test_client


def acting_as(client: TestClient, staff: dict[str, str], name: str) -> dict[str, str]:
    """Headers of ``staff`` impersonating the user ``name``."""
    made = client.post(
        f"/admin/auth/users/{_id(name)}/impersonate",
        json={"reason": "Ticket 42"},
        headers=staff,
    )
    assert made.status_code == 201, made.json()
    return bearer(made.json()["access_token"])


def test_staff_acting_as_a_user_neither_submit_nor_decide(client: TestClient) -> None:
    """Submitting and approving as an impersonated user are refused."""
    ann = user("ann", *CLERK)
    user("bob", *CLERK)
    sam = user("sam", *CLERK, "auth.view_user", "auth.change_user")

    async def make_staff() -> None:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key("sam")
            assert isinstance(found, AbstractUser)
            found.set_staff(True)
            await uow.commit()

    run_async(make_staff())
    books = open_books(client, sam)
    entry = sale(books, "big", "5000.00")

    as_ann = acting_as(client, sam, "ann")
    refused = submit(client, as_ann, "accounting.post_entry", entry)
    assert refused.status_code == 403

    made = submit(client, ann, "accounting.post_entry", entry)
    assert made.status_code == 201, made.json()
    as_bob = acting_as(client, sam, "bob")
    assert decide(client, made.json(), as_bob).status_code == 403
