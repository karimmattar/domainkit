"""Signing up by code, with an email address or a mobile number (ADR 0126)."""

from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

from fastapi.testclient import TestClient

from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.signup import send_signup_code
from domainkit.contrib.notifications.backends import sms_outbox
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import PASSWORD, audited, create_user
from domainkit_tests.test_auth_recovery import run_worker
from domainkit_tests.test_auth_reset_codes import (
    CODES,
    NUMBER,
    WRONG,
    App,
    code_in,
    drain,
    logs_in,
    other,
    texting,
)
from domainkit_tests.test_auth_signup import pending

__all__ = ["texting"]

BY_MAIL = {"SIGNUP_ENABLED": True, "VERIFICATION": "code", "IDENTIFIERS": ("email",)}
BY_TEXT = {"SIGNUP_ENABLED": True, **CODES}


def ask(client: TestClient, **body: object) -> Any:  # noqa: ANN401 - a response.
    """Ask for an account."""
    return client.post("/auth/signup", json=body)


def finish(
    client: TestClient, identifier: str, code: str, password: str = PASSWORD
) -> tuple[int, Any]:
    """The status and problem code of finishing a signup with ``code``."""
    response = client.post(
        "/auth/signup/code",
        json={"identifier": identifier, "code": code, "password": password},
    )
    problem = response.json().get("code") if response.content else None
    return response.status_code, problem


def stored(identifier: str) -> User:
    """The user ``identifier`` names."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.find_by_identifier(identifier)
        assert user is not None, identifier
        return cast(User, user)

    return run_async(read())


def test_a_mailed_code_makes_the_account(identity: Api) -> None:
    """No username: the address names the account, verified by the code."""
    client = identity(**BY_MAIL)

    response = ask(client, email="Nina@Example.com")
    assert response.status_code == 202, response.json()
    assert response.json() == {"detail": "Check your email to finish signing up."}
    (row,) = pending()
    assert (row["username"], row["mobile_number"]) == (None, None)
    assert row["expires_at"] - row["created_at"] == timedelta(minutes=10)
    run_worker()
    (message,) = mail.outbox
    assert (message.subject, message.to) == (
        "Your code to finish signing up",
        ("Nina@example.com",),
    )
    code = code_in(message.body)

    assert finish(client, "nina@example.com", other(code)) == WRONG
    assert finish(client, "nina@example.com", code, "short") == (
        422,
        "invalid_password",
    )
    assert finish(client, "NINA@example.com", code) == (204, None)
    assert finish(client, "nina@example.com", code) == WRONG
    assert pending() == []
    user = stored("nina@example.com")
    assert (user.username, user.email_verified) == (None, True)
    assert logs_in(client, "nina@example.com", PASSWORD)
    (refused, *_) = audited("auth.signup")
    assert refused["details"] == {"reason": "invalid_verification_code"}


def test_a_texted_code_makes_the_account(texting: App) -> None:
    """A number alone, where numbers identify users: the code goes by text."""
    client = texting(**BY_TEXT)

    response = ask(client, mobile_number="+33 6 12 34 56 78")
    assert response.json() == {
        "detail": "Check your text messages to finish signing up."
    }
    drain()
    (text,) = sms_outbox
    assert text.to == NUMBER
    assert "is your code to finish signing up." in text.body
    assert mail.outbox == []

    assert finish(client, "+33 (6) 12 34 56 78", code_in(text.body)) == (204, None)
    user = stored(NUMBER)
    assert (user.mobile_number_verified, user.email, user.username) == (True, "", None)
    assert logs_in(client, NUMBER, PASSWORD)


def test_a_signup_needs_somewhere_to_send_the_code(identity: Api) -> None:
    """An address; a number only with codes, where numbers identify users."""
    client = identity(**BY_MAIL)

    for body in ({}, {"username": "olga"}):
        response = ask(client, **body)
        assert (response.status_code, response.json()["code"]) == (
            422,
            "missing_value",
        )
    response = ask(client, mobile_number=NUMBER)
    assert (response.status_code, response.json()["code"]) == (
        422,
        "unknown_user_field",
    )
    numbers = identity(SIGNUP_ENABLED=True, IDENTIFIERS=("email", "mobile_number"))
    response = ask(numbers, email="pia@example.com", mobile_number="12")
    assert (response.status_code, response.json()["code"]) == (
        422,
        "invalid_mobile_number",
    )
    run_async(send_signup_code(uuid4()))  # A signup gone: nothing happens.
    assert mail.outbox == []


def test_a_taken_address_or_number_is_told_so(texting: App) -> None:
    """Nothing is made; the owner learns that someone tried."""
    create_user("quinn", email="quinn@example.com", mobile_number=NUMBER)
    client = texting(**BY_TEXT)

    ask(client, email="QUINN@example.com")
    ask(client, mobile_number=NUMBER)
    drain()
    (message,) = mail.outbox
    assert "Someone tried to sign up with this email address" in message.body
    (text,) = sms_outbox
    assert text.body.startswith("Someone tried to sign up with this number")
    assert pending() == []
    assert finish(client, NUMBER, "123456") == WRONG


def test_an_address_taken_before_the_code_is_used_answers_409(identity: Api) -> None:
    """The account came first: the code is spent, and nothing is made."""
    client = identity(**BY_MAIL)
    ask(client, email="rue@example.com")
    run_worker()
    code = code_in(mail.outbox[0].body)
    create_user("rue", email="rue@example.com")

    assert finish(client, "rue@example.com", code) == (409, "signup_taken")
    assert finish(client, "rue@example.com", code) == WRONG


def test_a_username_may_come_with_the_address(identity: Api) -> None:
    """The username is kept; the account logs in by address too."""
    client = identity(
        SIGNUP_ENABLED=True, VERIFICATION="code", IDENTIFIERS=("username", "email")
    )
    ask(client, email="sam@example.com", username="sam")
    run_worker()

    assert finish(client, "sam@example.com", code_in(mail.outbox[0].body)) == (
        204,
        None,
    )
    assert stored("sam").email == "sam@example.com"
    assert logs_in(client, "sam", PASSWORD)
