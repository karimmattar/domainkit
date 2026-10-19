"""Signing up: off by default, checked at once, finished by the worker."""

from typing import Any

from fastapi.testclient import TestClient

from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.signup import complete_signup
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import audited, bearer, create_user, login
from domainkit_tests.test_auth_recovery import (
    from_address,
    outbox_payloads,
    run_worker,
    token_in,
)

PASSWORD = "Sign-up-pass-77"


def signup(client: TestClient, **fields: str) -> Any:  # noqa: ANN401 - a response.
    """Ask for an account; ``fields`` replace alice's."""
    body = {"username": "alice", "email": "alice@example.com", "password": PASSWORD}
    return client.post("/auth/signup", json={**body, **fields})


def usernames() -> list[str]:
    """Every user's username, in order."""

    async def read() -> list[str]:
        async with AuthUnitOfWork() as uow:
            page = await uow.users.page(limit=100)
        return [user.get_username() for user in page.items]

    return run_async(read())


def test_signup_is_off_by_default(identity: Api) -> None:
    """The route answers 404 until ``AUTH.SIGNUP_ENABLED`` is on."""
    response = signup(identity())

    assert (response.status_code, response.json()["code"]) == (404, "not_found")
    assert outbox_payloads() == []


def test_signing_up_creates_the_account_and_verifies_its_address(
    identity: Api,
) -> None:
    """202 at once; the worker creates the user, then mails a link."""
    client = identity(SIGNUP_ENABLED=True)

    response = signup(client, username="Alice")

    assert response.status_code == 202
    assert response.json() == {"detail": "Check your email to finish signing up."}
    (payload,) = outbox_payloads()
    assert set(payload) == {"username", "email", "password_hash"}
    assert payload["password_hash"].startswith("pbkdf2_sha256$1$")
    assert PASSWORD not in str(payload)
    assert usernames() == []
    run_worker()  # Creates the account, and queues its verification.
    assert usernames() == ["Alice"]
    (message,) = mail.outbox
    assert (message.subject, message.to) == (
        "Verify your email address",
        ("alice@example.com",),
    )
    headers = bearer(login(client, "Alice", PASSWORD)["access_token"])
    assert client.get("/auth/me", headers=headers).json()["email_verified"] is False
    confirmed = client.post(
        "/auth/email-verification/confirm", json={"token": token_in(message)}
    )
    assert confirmed.status_code == 204
    assert client.get("/auth/me", headers=headers).json()["email_verified"] is True
    (signed_up,) = audited("auth.user_signed_up")
    (created,) = audited("auth.user_created")
    assert signed_up["details"] == {"user_id": created["details"]["user_id"]}


def test_taken_usernames_and_addresses_answer_the_same(identity: Api) -> None:
    """Nothing in the answer tells; the address given is told by mail."""
    create_user("alice", email="alice@example.com")
    client = identity(SIGNUP_ENABLED=True)

    fresh = signup(client, username="bob", email="bob@example.com")
    taken_name = signup(client, username="ALICE", email="carol@example.com")
    taken_address = signup(client, username="dave", email="Alice@Example.com")

    assert fresh.status_code == taken_name.status_code == taken_address.status_code
    assert fresh.json() == taken_name.json() == taken_address.json()
    run_worker()
    run_worker()
    assert usernames() == ["alice", "bob"]
    told = sorted((message.subject, message.to[0]) for message in mail.outbox)
    assert told == [
        ("Verify your email address", "bob@example.com"),
        ("Your sign-up", "Alice@Example.com"),
        ("Your sign-up", "carol@example.com"),
    ]
    exists = next(m for m in mail.outbox if m.to == ("carol@example.com",))
    assert "alice" not in exists.body.lower()


def test_fields_and_password_rules_are_checked_at_once(identity: Api) -> None:
    """Refusals that say nothing about other accounts come back as 422."""
    client = identity(SIGNUP_ENABLED=True)

    bad_name = signup(client, username="no spaces allowed")
    weak = signup(client, password="alice")
    no_address = signup(client, email="not-an-address")
    extra = client.post(
        "/auth/signup",
        json={
            "username": "a",
            "email": "a@example.com",
            "password": PASSWORD,
            "is_superuser": True,
        },
    )

    assert bad_name.status_code == weak.status_code == 422
    assert "username has 1 to 150" in bad_name.json()["detail"]
    assert "too similar to the username" in weak.json()["detail"]
    assert no_address.status_code == extra.status_code == 422
    assert outbox_payloads() == []


def test_signups_are_throttled_per_address_and_per_email(identity: Api) -> None:
    """``AUTH.EMAIL_RATE`` counts signups like other requests for mail."""
    client = identity(SIGNUP_ENABLED=True, EMAIL_RATE="1/hour")

    assert signup(client).status_code == 202
    by_address = signup(client, username="b", email="b@example.com")
    by_email = signup(
        from_address(client, "198.51.100.7"), username="c", email="ALICE@example.com"
    )

    assert by_address.status_code == by_email.status_code == 429
    refused = audited("auth.signup")  # The address's count, and alice's.
    assert [row["details"] for row in refused] == [{"reason": "throttled"}] * 2


def test_the_first_of_two_signups_for_a_name_wins(identity: Api) -> None:
    """The second finds the name taken, and its address is told."""
    identity(SIGNUP_ENABLED=True)

    run_async(complete_signup("alice", "alice@example.com", "pbkdf2_sha256$1$s$h"))
    run_async(complete_signup("alice", "other@example.com", "pbkdf2_sha256$1$s$h"))

    assert usernames() == ["alice"]
    (message,) = mail.outbox
    assert (message.subject, message.to) == ("Your sign-up", ("other@example.com",))
