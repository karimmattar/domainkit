"""Logging in with an email address, a mobile number, or a username (ADR 0126)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from domainkit.conf import override_settings
from domainkit.contrib.auth import (
    AuthUnitOfWork,
    EmailTaken,
    IdentifierRequired,
    authenticate,
)
from domainkit.contrib.auth.models import UserId
from domainkit.contrib.auth.settings import auth_settings
from domainkit.contrib.auth.throttling import account_digest
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_admin import ACCOUNTS, swapped
from domainkit_tests.test_auth_http import PASSWORD, create_user

__all__ = ["swapped"]

EVERY = ("email", "mobile_number", "username")


def log_in(client: TestClient, **body: str) -> int:
    """The status a login with ``body`` and the password gets."""
    response = client.post("/auth/login", json={**body, "password": PASSWORD})
    return response.status_code


def identified_by(*fields: str) -> dict[str, Any]:
    """The ``AUTH`` settings in use, with ``IDENTIFIERS`` set to ``fields``."""
    return {**auth_settings().model_dump(exclude_unset=True), "IDENTIFIERS": fields}


def found(value: str) -> Any:  # noqa: ANN401 - a user, of the model in use.
    """The user ``value`` identifies, or None."""

    async def find() -> Any:  # noqa: ANN401 - as above.
        async with AuthUnitOfWork() as uow:
            return await uow.users.find_by_identifier(value)

    return run_async(find())


def test_a_user_logs_in_with_each_identifier(api: Api) -> None:
    """An address in any case, a number however written, or a username."""
    client = api(IDENTIFIERS=EVERY)
    create_user(None, email="Ann@Example.com", mobile_number="+33 6 12 34 56 78")
    create_user("bob")

    assert log_in(client, identifier="ann@example.COM") == 200
    assert log_in(client, identifier="+33 (6) 12-34-56-78") == 200
    assert log_in(client, username="+33612345678") == 200
    assert log_in(client, identifier="bob") == 200
    assert log_in(client, identifier="ann") == 401
    assert log_in(client, identifier="+33600000000") == 401


def test_only_the_identifiers_named_log_in(api: Api) -> None:
    """A field left out of ``AUTH.IDENTIFIERS`` identifies no one."""
    client = api(IDENTIFIERS=("email",))
    create_user("carol", email="carol@example.com", mobile_number="+33611111111")

    assert log_in(client, identifier="carol@example.com") == 200
    assert log_in(client, identifier="carol") == 401
    assert log_in(client, identifier="+33611111111") == 401


def test_by_default_the_username_alone_logs_in(api: Api) -> None:
    """As before 1.8: ``username`` names the user, exactly."""
    client = api()
    create_user("dave", email="dave@example.com")

    assert log_in(client, username="dave") == 200
    assert log_in(client, identifier="dave") == 200
    assert log_in(client, identifier="dave@example.com") == 401
    assert log_in(client, identifier="Dave") == 401


def test_an_address_two_users_share_identifies_neither(api: Api) -> None:
    """Rows stored before addresses identified users may repeat one."""
    api()
    create_user("erin", email="shared@example.com")
    create_user("frank", email="SHARED@example.com")

    with override_settings(AUTH=identified_by("email")):
        assert found("shared@example.com") is None
        assert found("erin") is None


def test_a_backend_reads_the_identifier(api: Api) -> None:
    """``identifier`` first; ``username`` and the ``USERNAME_FIELD`` too."""
    api(IDENTIFIERS=EVERY)
    create_user(None, mobile_number="+33622222222")

    def check(**credentials: object) -> bool:
        return run_async(authenticate(**credentials)) is not None

    assert check(identifier="+33622222222", password=PASSWORD)
    assert check(username="+33622222222", password=PASSWORD)
    assert check(identifier="+33622222222", username="x", password=PASSWORD)
    assert not check(identifier=7, password=PASSWORD)


def test_an_address_identifies_one_user(api: Api) -> None:
    """A second user, or a change, to an address taken in any case: 409."""
    client = api(IDENTIFIERS=("email", "mobile_number"))
    create_user(None, email="gina@example.com")

    with pytest.raises(EmailTaken) as taken:
        create_user(None, email="GINA@example.com")
    assert taken.value.code == "email_taken"
    assert taken.value.fields == ("email",)

    other = create_user(None, email="hal@example.com")

    async def change(email: str) -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(other))
            user.update(email=email)
            await uow.commit()

    with pytest.raises(EmailTaken):
        run_async(change("Gina@Example.com"))
    run_async(change("HAL@example.com"))  # Its own address, in another case.
    assert log_in(client, identifier="hal@example.com") == 200


def test_a_new_user_needs_an_identifier_the_project_names(api: Api) -> None:
    """``AUTH.IDENTIFIERS`` names what a new user must have, one at least."""
    api()
    with pytest.raises(IdentifierRequired) as refused:
        create_user(None, email="kim@example.com")
    assert refused.value.message == (
        "Give one of these, which identify users: username."
    )
    api(IDENTIFIERS=("email", "mobile_number"))
    with pytest.raises(IdentifierRequired):
        create_user("lou")
    create_user(None, mobile_number="+33633333333")


def test_an_address_repeats_where_it_identifies_no_one(api: Api) -> None:
    """Without ``email`` in ``AUTH.IDENTIFIERS``, as before 1.8."""
    api()
    create_user("ian", email="team@example.com")
    create_user("jo", email="team@example.com")


def test_one_number_written_two_ways_counts_as_one(api: Api) -> None:
    """Wrong logins at a number count against it however it is written."""
    api()
    assert account_digest("+33 6 12 34 56 78") == account_digest("+33612345678")
    assert account_digest(" Ann@Example.com ") == account_digest("ann@example.com")
    assert account_digest("bob") != account_digest("+33612345678")


@pytest.mark.parametrize("swapped", [ACCOUNTS], indirect=True)
def test_a_field_of_the_projects_own_identifies_users(swapped: TestClient) -> None:
    """Values in another field's form are not tried against it."""
    client = swapped
    with override_settings(AUTH=identified_by("email", "phone")):
        create_user("kim", email="kim@example.com", phone="kim-desk")
        create_user("lee", email="lee@example.com", phone="desk@lee")

        assert log_in(client, identifier="kim-desk") == 200
        assert log_in(client, identifier="kim@example.com") == 200
        assert found("desk@lee") is None
        assert log_in(client, identifier="kim") == 401
