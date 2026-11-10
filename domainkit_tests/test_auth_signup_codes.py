"""Signing up by code, with an email address or a mobile number (ADR 0126)."""

from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.codes import EMAIL_VERIFICATION, target_of
from domainkit.contrib.auth.models import UserId
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

BY_MAIL = {"SIGNUP_ENABLED": True, "IDENTIFIERS": ("email",)}
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
    assert "never proved" not in message.body
    assert "\n\n\n" not in message.body

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
    """Proved by their account: nothing is made; the owner learns of the try."""
    create_user(
        "quinn",
        email="quinn@example.com",
        mobile_number=NUMBER,
        email_verified=True,
        mobile_number_verified=True,
    )
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
    """An account proved it first: the code is spent, and nothing is made."""
    client = identity(**BY_MAIL)
    ask(client, email="rue@example.com")
    run_worker()
    code = code_in(mail.outbox[0].body)
    create_user("rue", email="rue@example.com", email_verified=True)

    assert finish(client, "rue@example.com", code) == (409, "signup_taken")
    assert finish(client, "rue@example.com", code) == WRONG


def test_a_username_may_come_with_the_address(identity: Api) -> None:
    """The username is kept; the account logs in by address too."""
    client = identity(SIGNUP_ENABLED=True, IDENTIFIERS=("username", "email"))
    ask(client, email="sam@example.com", username="sam")
    run_worker()

    assert finish(client, "sam@example.com", code_in(mail.outbox[0].body)) == (
        204,
        None,
    )
    assert stored("sam").email == "sam@example.com"
    assert logs_in(client, "sam", PASSWORD)


# An account that never proved its address or number (ADR 0133).


def test_an_address_held_unproved_moves_to_the_signup_that_proves_it(
    identity: Api,
) -> None:
    """The holder keeps its username, loses the address and its codes."""
    client = identity(SIGNUP_ENABLED=True, IDENTIFIERS=("username", "email"))
    holder = create_user("ivy", email="Ivy@example.com")
    held_code = run_async(_code_for(holder, "ivy@example.com"))

    ask(client, email="ivy@example.com")
    run_worker()
    (message,) = mail.outbox
    assert "An account already uses this address, but has never proved it" in (
        message.body
    )
    assert "\n\n\n" not in message.body
    assert finish(client, "ivy@example.com", code_in(message.body)) == (204, None)

    signed_up = stored("ivy@example.com")
    assert (signed_up.id != holder, signed_up.email_verified) == (True, True)
    kept = _user(holder)
    assert (kept.email, kept.is_active, kept.username) == ("", True, "ivy")
    assert logs_in(client, "ivy", PASSWORD)
    assert logs_in(client, "ivy@example.com", PASSWORD)  # The new account.
    assert not run_async(_still_works(holder, "ivy@example.com", held_code))
    (released,) = audited("auth.email_released")
    assert released["details"]["user_id"] == str(holder)


def test_a_holder_left_with_nothing_to_log_in_by_is_ended(identity: Api) -> None:
    """Addresses alone log in: the holder is deactivated, its logins end."""
    client = identity(**BY_MAIL)
    holder = create_user(None, email="jo@example.com")
    assert logs_in(client, "jo@example.com", PASSWORD)

    ask(client, email="jo@example.com")
    run_worker()
    assert finish(client, "jo@example.com", code_in(mail.outbox[0].body)) == (
        204,
        None,
    )

    ended = _user(holder)
    assert (ended.email, ended.is_active, ended.has_usable_password()) == (
        "",
        False,
        False,
    )
    assert stored("jo@example.com").id != holder


def test_a_number_held_unproved_moves_too(texting: App) -> None:
    """The number is unique: it moves to the new account, verified."""
    client = texting(**BY_TEXT)
    holder = create_user(None, email="kai@example.com", mobile_number=NUMBER)

    ask(client, mobile_number=NUMBER)
    drain()
    (text,) = sms_outbox
    assert finish(client, NUMBER, code_in(text.body)) == (204, None)

    assert stored(NUMBER).mobile_number_verified is True
    kept = _user(holder)
    assert (kept.mobile_number, kept.is_active, kept.email) == (
        None,
        True,
        "kai@example.com",
    )
    assert len(audited("auth.mobile_number_released")) == 1


def test_a_holder_that_proves_it_first_keeps_it(identity: Api) -> None:
    """Proved between the mail and the code: the signup answers 409."""
    client = identity(**BY_MAIL)
    holder = create_user(None, email="lu@example.com")
    ask(client, email="lu@example.com")
    run_worker()
    code = code_in(mail.outbox[0].body)
    run_async(_verify(holder))

    assert finish(client, "lu@example.com", code) == (409, "signup_taken")
    assert _user(holder).email == "lu@example.com"


async def _code_for(user_id: UUID, address: str) -> str:
    """An email verification code for ``user_id``, sent to ``address``."""
    async with AuthUnitOfWork() as uow:
        code = await uow.verification_codes.issue(
            EMAIL_VERIFICATION, target_of(address), user_id=user_id
        )
        await uow.commit()
    return code


async def _still_works(user_id: UUID, address: str, code: str) -> bool:
    """Whether ``code`` still proves ``address`` for ``user_id``."""
    async with AuthUnitOfWork() as uow:
        proven = await uow.verification_codes.check(
            EMAIL_VERIFICATION, target_of(address), code
        )
    return proven is not None and proven.user_id == user_id


async def _verify(user_id: UUID) -> None:
    """Mark ``user_id``'s address proved."""
    async with AuthUnitOfWork() as uow:
        user = cast(User, await uow.users.get(UserId(user_id)))
        user.verify_email()
        await uow.commit()


def _user(user_id: UUID) -> User:
    """The user ``user_id``, as stored."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            return cast(User, await uow.users.get(UserId(user_id)))

    return run_async(read())
