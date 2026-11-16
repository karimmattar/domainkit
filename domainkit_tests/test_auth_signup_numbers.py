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
