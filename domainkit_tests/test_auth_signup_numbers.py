"""A number given with an address at signup: proved by its own code (ADR 0171).

The signup's code goes to the address, and proves the address alone. Until
2.5 the signup took the number too, unproved: from an account that held
it unproved, which ended when the number was its only login, and onto the
new account. And which mail the address got told whether an account used
the number.
"""

from typing import Final, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.models import UserId
from domainkit.contrib.notifications.backends import sms_outbox
from domainkit.management.base import run_async
from domainkit_tests.support.auth import PASSWORD, audited, bearer, create_user, login
from domainkit_tests.test_auth_recovery import from_address
from domainkit_tests.test_auth_reset_codes import (
    App,
    code_in,
    drain,
    logs_in,
    texting,
)
from domainkit_tests.test_auth_signup_codes import ask, finish, stored

__all__ = ["texting"]

NUMBER: Final = "+33612345678"
BOTH: Final = {"SIGNUP_ENABLED": True, "IDENTIFIERS": ("email", "mobile_number")}
CONFIRM: Final = "Your code to finish signing up"


def user(user_id: UUID) -> User:
    """The user, as stored."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            return cast(User, await uow.users.get(UserId(user_id)))

    return run_async(read())


def signed_up(client: TestClient, address: str, number: str = NUMBER) -> None:
    """Sign up with ``address`` and ``number``, and use the code mailed."""
    assert ask(client, email=address, mobile_number=number).status_code == 202
    drain()
    (message,) = [m for m in mail.outbox if m.to == (address,)]
    assert finish(client, address, code_in(message.body)) == (204, None)
    drain()  # The number's own code.


def test_a_signup_takes_no_number_it_did_not_prove(texting: App) -> None:
    """Mallory gives her address and the number of an account that never proved it.

    A superuser that ``createsuperuser --mobile_number`` made, say. The
    account keeps its number, and its login; Mallory's account gets no
    number, and the code to prove it goes to the number's reader.
    """
    client = texting(**BOTH)
    victim = create_user(None, mobile_number=NUMBER)  # Its only login.

    signed_up(client, "mallory@evil.test")

    kept = user(victim)
    assert (kept.mobile_number, kept.is_active, kept.has_usable_password()) == (
        NUMBER,
        True,
        True,
    )
    assert logs_in(client, NUMBER, PASSWORD)
    mallory = stored("mallory@evil.test")
    assert (mallory.email_verified, mallory.mobile_number) == (True, None)
    assert audited("auth.mobile_number_released") == []
    (text,) = sms_outbox
    assert text.to == NUMBER
    assert "is your code to verify your number." in text.body


@pytest.mark.parametrize("holder", ["none", "unproved", "proved"])
def test_the_signups_mail_says_nothing_of_the_number(texting: App, holder: str) -> None:
    """The same code mail whoever holds the number: no oracle, no wrong text.

    A number an account proved got ``Your sign-up`` (an account uses it)
    instead of a code; one an account held unproved got the mail saying an
    account held the address.
    """
    client = texting(**BOTH)
    if holder != "none":
        create_user(
            None,
            email="holder@example.com",
            mobile_number=NUMBER,
            mobile_number_verified=holder == "proved",
        )

    assert ask(client, email="probe@example.com", mobile_number=NUMBER).json() == {
        "detail": "Check your email to finish signing up."
    }
    drain()

    (message,) = mail.outbox
    assert message.subject == CONFIRM
    assert "An account already uses this address" not in message.body
    assert sms_outbox == []  # Nothing goes to the number before the address.


def test_the_number_is_proved_by_its_own_code(texting: App) -> None:
    """The address's code makes the account; the number's code adds the number."""
    client = texting(**BOTH)

    signed_up(client, "ann@example.com")
    before = stored("ann@example.com")
    (text,) = sms_outbox
    headers = bearer(login(client, "ann@example.com")["access_token"])
    proved = client.post(
        "/auth/mobile-verification/code",
        json={"code": code_in(text.body)},
        headers=headers,
    )

    assert before.mobile_number is None
    assert proved.status_code == 204, proved.json()
    after = stored("ann@example.com")
    assert (after.mobile_number, after.mobile_number_verified) == (NUMBER, True)
    assert logs_in(client, NUMBER, PASSWORD)


def test_a_number_another_account_proved_is_told_to_its_reader(
    texting: App,
) -> None:
    """Only whoever reads the number learns that an account uses it."""
    client = texting(**BOTH)
    owner = create_user(None, mobile_number=NUMBER, mobile_number_verified=True)

    signed_up(client, "ann@example.com")
    (text,) = sms_outbox
    headers = bearer(login(client, "ann@example.com")["access_token"])
    taken = client.post(
        "/auth/mobile-verification/code",
        json={"code": code_in(text.body)},
        headers=headers,
    )

    assert (taken.status_code, taken.json()["code"]) == (409, "mobile_number_taken")
    assert user(owner).mobile_number == NUMBER
    assert stored("ann@example.com").mobile_number is None


def test_a_signups_number_is_texted_within_the_rate(texting: App) -> None:
    """``AUTH.EMAIL_RATE`` per number, as ``/mobile-verification`` counts it.

    Signups with addresses of one's own would text anyone's number
    otherwise. Past the rate, the account is made without it.
    """
    client = texting(**BOTH, EMAIL_RATE="1/hour")

    signed_up(client, "one@example.com")
    signed_up(from_address(client, "198.51.100.7"), "two@example.com")

    assert [text.to for text in sms_outbox] == [NUMBER]
    assert stored("two@example.com").mobile_number is None


def test_a_signup_by_number_alone_is_unchanged(texting: App) -> None:
    """Its code goes to the number, and proves it; an unproved holder loses it."""
    client = texting(**BOTH)
    holder = create_user(None, email="kai@example.com", mobile_number=NUMBER)

    assert ask(client, mobile_number=NUMBER).status_code == 202
    drain()
    (text,) = sms_outbox

    assert finish(client, NUMBER, code_in(text.body)) == (204, None)
    assert stored(NUMBER).mobile_number_verified
    assert user(holder).mobile_number is None


def test_an_old_number_stays_with_an_account_until_its_reader_proves_it(
    texting: App,
) -> None:
    """One holder of both, unproved: the address moves, the number stays.

    The holder keeps a login (its number), so it is not ended.
    """
    client = texting(**BOTH)
    holder = create_user(None, email="max@example.com", mobile_number=NUMBER)

    signed_up(client, "max@example.com")

    kept = user(holder)
    assert (kept.email, kept.mobile_number, kept.is_active) == ("", NUMBER, True)
    made = stored("max@example.com")
    assert (made.email_verified, made.mobile_number) == (True, None)


# Second review of user report A12 ---------------------------------------------------


@pytest.mark.parametrize(
    ("identifiers", "body"),
    [
        (("mobile_number",), {}),
        (("username", "mobile_number"), {}),
    ],
)
def test_where_no_address_logs_in_the_number_gets_the_code(
    texting: App, identifiers: tuple[str, ...], body: dict[str, str]
) -> None:
    """Made with the address alone, the account could not log in: 422 for ever.

    Where neither the address nor a username given identifies users, the
    code goes to the number and proves it; the address is held for a code
    of its own, which ``/email-verification/code`` takes.
    """
    client = texting(SIGNUP_ENABLED=True, IDENTIFIERS=identifiers)

    asked = ask(client, email="nina@example.com", mobile_number=NUMBER, **body)
    drain()
    (text,) = sms_outbox
    done = finish(client, NUMBER, code_in(text.body))
    drain()
    (mailed,) = mail.outbox
    headers = bearer(login(client, NUMBER)["access_token"])
    proved = client.post(
        "/auth/email-verification/code",
        json={"code": code_in(mailed.body)},
        headers=headers,
    )

    assert asked.json() == {"detail": "Check your text messages to finish signing up."}
    assert "is your code to finish signing up." in text.body
    assert done == (204, None)
    assert mailed.subject == "Your code to verify your email address"
    assert proved.status_code == 204, proved.json()
    nina = stored(NUMBER)
    assert (nina.mobile_number_verified, nina.email, nina.email_verified) == (
        True,
        "nina@example.com",
        True,
    )


def test_a_username_that_logs_in_lets_the_address_get_the_code(
    texting: App,
) -> None:
    """With a username that identifies users, the address may take the code."""
    client = texting(SIGNUP_ENABLED=True, IDENTIFIERS=("username", "mobile_number"))

    asked = ask(client, username="nina", email="nina@example.com", mobile_number=NUMBER)

    assert asked.json() == {"detail": "Check your email to finish signing up."}


def test_a_required_number_comes_on_its_own_never_under_fields(
    texting: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``REQUIRED_FIELDS`` naming ``mobile_number`` put it under ``fields``.

    There it was stored unproved, nothing was texted, and the 409 or 204
    of ``/signup/code`` told whether an account had it.
    """
    monkeypatch.setattr(User, "REQUIRED_FIELDS", ("email", "mobile_number"))
    client = texting(**BOTH)

    under_fields = ask(
        client,
        email="m@evil.test",
        mobile_number=NUMBER,
        fields={"mobile_number": NUMBER},
    )
    missing = ask(client, email="m@evil.test")
    signed_up(client, "m@evil.test")

    assert (under_fields.status_code, under_fields.json()["code"]) == (
        422,
        "unknown_user_field",
    )
    assert (missing.status_code, missing.json()["code"]) == (422, "invalid_user_field")
    assert stored("m@evil.test").mobile_number is None
    assert [text.to for text in sms_outbox] == [NUMBER]  # Its own code.


def test_asking_again_counts_the_held_number_too(texting: App) -> None:
    """``POST /mobile-verification`` with no body re-texts the held number.

    It was counted per user and per client address only, so accounts made
    with someone's number, each asking again, could text it without bound.
    """
    client = texting(**BOTH, EMAIL_RATE="2/hour")
    signed_up(client, "a@evil.test")
    signed_up(from_address(client, "198.51.100.7"), "b@evil.test")
    answers = []
    for index, who in enumerate(["a@evil.test", "a@evil.test", "b@evil.test"]):
        other = from_address(client, f"203.0.113.{index + 1}")
        headers = bearer(login(other, who)["access_token"])
        answers.append(other.post("/auth/mobile-verification", headers=headers))
        drain()
    other = from_address(client, "203.0.113.9")
    headers = bearer(login(other, "b@evil.test")["access_token"])
    answers.append(other.post("/auth/mobile-verification", headers=headers))

    assert [answer.status_code for answer in answers] == [202, 202, 429, 429]
    assert len(sms_outbox) == 4  # The two signups', and two asked again.


def test_one_users_code_never_replaces_anothers(texting: App) -> None:
    """Mallory's signup with the owner's number leaves the owner's code working.

    Codes were one per purpose and number, whoever asked.
    """
    client = texting(**BOTH)
    signed_up(client, "owner@example.com")
    owners = code_in(sms_outbox[-1].body)
    signed_up(from_address(client, "198.51.100.9"), "m@evil.test")
    headers = bearer(login(client, "owner@example.com")["access_token"])

    proved = client.post(
        "/auth/mobile-verification/code", json={"code": owners}, headers=headers
    )

    assert proved.status_code == 204, proved.json()
    assert stored("owner@example.com").mobile_number == NUMBER


def test_signups_spend_no_count_of_the_numbers_owner(texting: App) -> None:
    """Two signups with the owner's number leave the owner's own asking alone."""
    client = texting(**BOTH, EMAIL_RATE="2/hour")
    create_user("owner", email="owner@example.com", proved=True)
    signed_up(from_address(client, "198.51.100.1"), "a@evil.test")
    signed_up(from_address(client, "198.51.100.2"), "b@evil.test")
    other = from_address(client, "203.0.113.50")
    headers = bearer(login(other, "owner@example.com")["access_token"])

    asked = other.post(
        "/auth/mobile-verification", json={"mobile_number": NUMBER}, headers=headers
    )

    assert asked.status_code == 202, asked.json()


def test_holding_runs_the_manage_logins_checks(texting: App) -> None:
    """As ``POST /mobile-verification`` does: refused, the number is not held."""
    from fastapi import status
    from starlette.requests import Request

    from domainkit.contrib.auth import AbstractBaseUser
    from domainkit.contrib.auth.gates import on_manage_logins
    from domainkit.http.errors import APIError

    client = texting(**BOTH)
    seen: list[str] = []

    async def refuse(request: Request, user: AbstractBaseUser) -> None:
        seen.append(user.get_username())
        raise APIError(status.HTTP_403_FORBIDDEN, "Not here.", code="refused")

    on_manage_logins(refuse, domain="auth")
    signed_up(client, "ann@example.com")

    assert seen == ["ann@example.com"]
    assert sms_outbox == []
    assert stored("ann@example.com").mobile_number is None
