"""SEC-15b-1: with SUPERUSER_APPROVALS, two non-superusers make a superuser.

Without approvals, only a superuser makes a superuser (the auth admin
API's ``_superusers_only``). ``SUPERUSER_APPROVALS`` is meant to add a
checker to that, and the docs say "A superuser submits
``auth.grant_superuser``". But the guard's maker permission is
``SUPERUSER_APPROVERS`` (``auth.change_user`` by default), so any user
holding ``auth.change_user`` and ``approvals.add_approvalrequest`` (not
a superuser, not even staff) submits ``{"user_id": <themselves>}``, and
any other holder of ``auth.change_user`` approves it: the worker makes
the maker a superuser. The approver may even be the user it promotes.

The test asserts the safe behaviour (the maker is not made a superuser)
and fails on ``d23a2ec``.
"""

from fastapi.testclient import TestClient

from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.messaging.testing import drain
from domainkit.management.base import run_async
from domainkit_tests.test_approvals_api import (  # noqa: F401 - the fixture.
    _id,
    client,
    decide,
    submit,
    user,
)


def _is_superuser(username: str) -> bool:
    """Whether ``username`` is a superuser now."""
    user_id = _id(username)

    async def read() -> bool:
        async with AuthUnitOfWork() as uow:
            return (await uow.users.get(user_id)).is_superuser  # type: ignore[arg-type]

    return run_async(read())


def test_two_non_superusers_cannot_make_one_of_them_a_superuser(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Neither is a superuser or staff; they hold ``auth.change_user`` only."""
    mallory = user(
        "mallory",
        "auth.change_user",
        "approvals.add_approvalrequest",
        "approvals.view_approvalrequest",
    )
    trent = user("trent", "auth.change_user", "approvals.view_approvalrequest")
    assert not _is_superuser("mallory")

    made = submit(
        client, mallory, "auth.grant_superuser", {"user_id": str(_id("mallory"))}
    )
    if made.status_code == 201:
        decide(client, made.json(), trent)
        run_async(drain())

    assert not _is_superuser("mallory"), (
        "two users who are not superusers made one of them a superuser"
    )
