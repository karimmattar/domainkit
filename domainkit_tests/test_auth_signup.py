"""Signing up: off by default, checked at once, made when the address confirms."""

from datetime import timedelta
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from domainkit import signing
from domainkit.conf import override_settings
from domainkit.contrib import mail
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.repositories import PendingSignup, SignupStore
from domainkit.contrib.auth.signup import (
    send_signup_link,
    signup_of,
    signup_token,
)
from domainkit.contrib.auth.tables import pending_signups
from domainkit.contrib.auth.tokens import utc_now
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import audited, bearer, create_user, login
from domainkit_tests.test_auth_recovery import (
    from_address,
    outbox_payloads,
    outbox_targets,
    run_worker,
    token_in,
)

PASSWORD = "Sign-up-pass-77"
CONFIRM = "Confirm your sign-up"
EXISTS = "Your sign-up"


def signup(client: TestClient, **fields: str) -> Any:  # noqa: ANN401 - a response.
    """Ask for an account; ``fields`` replace alice's."""
    body = {"username": "alice", "email": "alice@example.com"}
    return client.post("/auth/signup", json={**body, **fields})


def confirm(client: TestClient, token: str, password: str = PASSWORD) -> Any:  # noqa: ANN401 - a response.
    """Use a signup link's token, choosing ``password``."""
    return client.post(
        "/auth/signup/confirm", json={"token": token, "password": password}
    )


def usernames() -> list[str]:
    """Every user's username, in order."""

    async def read() -> list[str]:
        async with AuthUnitOfWork() as uow:
            page = await uow.users.page(limit=100)
        return [user.get_username() for user in page.items]

    return run_async(read())


def pending() -> list[dict[str, Any]]:
    """Every pending signup's row."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(pending_signups))
            return [dict(row._mapping) for row in rows]

    return run_async(read())


def mails() -> list[tuple[str, str]]:
    """The subject and the address of each mail sent, sorted."""
    return sorted((message.subject, message.to[0]) for message in mail.outbox)


def link_to(address: str) -> str:
    """The token in the last signup link mailed to ``address``."""
    (message, *_) = [
        message
        for message in reversed(mail.outbox)
        if message.to == (address,) and message.subject == CONFIRM
    ]
    return token_in(message)


def test_signup_is_off_by_default(identity: Api) -> None:
    """The route answers 404 until ``AUTH.SIGNUP_ENABLED`` is on."""
    response = signup(identity())

    assert (response.status_code, response.json()["code"]) == (404, "not_found")
    assert response.json()["detail"] == "Not Found"
    assert outbox_payloads() == []


def test_an_account_is_made_when_its_address_is_confirmed(identity: Api) -> None:
    """202 at once; the worker mails a link; the link makes the account."""
    client = identity(SIGNUP_ENABLED=True)

    response = signup(client, username="Alice")

    assert response.status_code == 202
    assert response.json() == {"detail": "Check your email to finish signing up."}
    (stored,) = pending()
    assert set(stored) == {"id", "username", "email", "created_at", "expires_at"}
    assert (stored["username"], stored["email"]) == ("Alice", "alice@example.com")
    assert stored["expires_at"] - stored["created_at"] == timedelta(days=3)
    # The outbox holds the pending signup's id alone: no hash, no address.
    assert outbox_payloads() == [{"signup_id": str(stored["id"])}]
    assert outbox_targets() == ["domainkit.auth.send_signup_link"]
    run_worker()
    (message,) = mail.outbox
    assert (message.subject, message.to) == (CONFIRM, ("alice@example.com",))
    assert "choose your password within 3 days" in message.body
    assert "Alice" not in message.body  # A name the address's owner never chose.
    assert usernames() == []  # No account until the link is used.
    refused = client.post(
        "/auth/login", json={"username": "Alice", "password": PASSWORD}
    )
    assert refused.status_code == 401

    confirmed = confirm(client, token_in(message))

    assert confirmed.status_code == 204
    assert usernames() == ["Alice"]
    assert pending() == []
    headers = bearer(login(client, "Alice", PASSWORD)["access_token"])
    me = client.get("/auth/me", headers=headers).json()
    assert (me["email"], me["email_verified"]) == ("alice@example.com", True)
    (signed_up,) = audited("auth.user_signed_up")
    (created,) = audited("auth.user_created")
    assert signed_up["details"] == {"user_id": created["details"]["user_id"]}
    assert len(audited("auth.email_verified")) == 1
    used = confirm(client, token_in(message))  # A link works once.
    assert (used.status_code, used.json()["code"]) == (400, "invalid_token")


def test_signups_tell_nobody_else_whether_an_address_has_an_account(
    identity: Api,
) -> None:
    """Asking with someone's address, then logging in, shows nothing.

    An account used to be made at once when the address was free: logging
    in with the username chosen answered 200 then, and 401 when the address
    had an account. Now neither has an account until the address's owner
    confirms, and a pending signup reserves no username.
    """
    create_user("victim", email="victim@example.com")
    client = identity(SIGNUP_ENABLED=True)

    taken = signup(client, username="probe1", email="victim@example.com")
    free = signup(client, username="probe2", email="nobody@example.com")
    run_worker()
    logins = [
        client.post("/auth/login", json={"username": name, "password": PASSWORD})
        for name in ("probe1", "probe2")
    ]
    again = [
        signup(
            from_address(client, "198.51.100.9"),
            username=name,
            email="prober@example.com",
        )
        for name in ("probe1", "probe2")
    ]
    run_worker()

    assert taken.status_code == free.status_code == 202
    assert taken.json() == free.json()
    assert [response.status_code for response in logins] == [401, 401]
    assert [response.status_code for response in again] == [202, 202]
    assert mails() == [
        (CONFIRM, "nobody@example.com"),
        (CONFIRM, "prober@example.com"),
        (CONFIRM, "prober@example.com"),
        (EXISTS, "victim@example.com"),
    ]
    assert usernames() == ["victim"]


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
    assert mails() == [
        (CONFIRM, "bob@example.com"),
        (EXISTS, "Alice@example.com"),
        (EXISTS, "carol@example.com"),
    ]
    exists = next(m for m in mail.outbox if m.to == ("carol@example.com",))
    assert "alice" not in exists.body.lower()
    assert "sign up with this email address (carol@example.com)" in exists.body
    assert [row["username"] for row in pending()] == ["bob"]  # The others are gone.
    assert confirm(client, link_to("bob@example.com")).status_code == 204
    assert usernames() == ["alice", "bob"]


def test_fields_are_checked_at_once(identity: Api) -> None:
    """Refusals that say nothing about other accounts come back as 422.

    A password is not taken here: whoever reads the mail chooses it.
    """
    client = identity(SIGNUP_ENABLED=True)

    bad_name = signup(client, username="no spaces allowed")
    kept = signup(client, username="Erased-0123456789abcdef0123456789abcdef")
    no_address = signup(client, email="not-an-address")
    with_password = signup(client, password=PASSWORD)
    extra = signup(client, is_superuser="true")

    assert bad_name.status_code == 422
    assert "username has 1 to 150" in bad_name.json()["detail"]
    assert (kept.status_code, kept.json()["code"]) == (422, "invalid_username")
    assert no_address.status_code == with_password.status_code == 422
    assert extra.status_code == 422
    assert with_password.json()["errors"][0]["loc"] == ["body", "password"]
    assert outbox_payloads() == []


def test_the_password_is_chosen_and_checked_when_confirming(identity: Api) -> None:
    """The validators check it against the account; a refusal keeps the link."""
    client = identity(SIGNUP_ENABLED=True)
    signup(client)
    run_worker()
    token = link_to("alice@example.com")

    weak = confirm(client, token, password="alice")
    empty = client.post("/auth/signup/confirm", json={"token": token})
    chosen = confirm(client, token, password=PASSWORD)

    assert (weak.status_code, weak.json()["code"]) == (422, "invalid_password")
    assert "too similar to the username" in weak.json()["detail"]
    assert empty.status_code == 422
    assert chosen.status_code == 204
    assert login(client, "alice", PASSWORD)["access_token"]


def test_whoever_asks_with_another_address_gains_nothing(identity: Api) -> None:
    """No pre-account hijacking: the address's owner chooses the password.

    An attacker asked for an account with the victim's address and a
    password of their own; when the victim used the link mailed to them,
    the account was made with the attacker's password, which the attacker
    then logged in with. Now no password is taken when asking, and the
    victim chooses it with the link.
    """
    client = identity(SIGNUP_ENABLED=True)
    guess = "Attacker-pass-91"
    attacker = from_address(client, "203.0.113.66")

    with_password = signup(
        attacker, username="victim", email="victim@example.com", password=guess
    )
    asked = signup(attacker, username="victim", email="victim@example.com")
    run_worker()
    confirmed = confirm(client, link_to("victim@example.com"), password=PASSWORD)
    attempts = [
        attacker.post("/auth/login", json={"username": "victim", "password": guess}),
        client.post("/auth/login", json={"username": "victim", "password": PASSWORD}),
    ]

    assert with_password.status_code == 422
    assert asked.status_code == 202
    assert confirmed.status_code == 204
    assert [response.status_code for response in attempts] == [401, 200]


@pytest.mark.parametrize(
    "email",
    [
        "ceo\uff20corp.example<eve@evil.example>",  # Mail would go to eve.
        "x0<victim@example.com>",  # Each name would get its own EMAIL_RATE count.
        "\u212aelly@corp.example",  # The KELVIN SIGN, a k to lower().
        "kelly@corp.example(Kelly)",
    ],
)
def test_signups_take_plain_addresses_only(identity: Api, email: str) -> None:
    """One ASCII address alone, which mail goes to as the account shows it."""
    client = identity(SIGNUP_ENABLED=True)

    response = signup(client, email=email)

    assert response.status_code == 422
    (refused,) = response.json()["errors"]
    assert (refused["loc"], refused["msg"]) == (
        ["body", "email"],
        (
            "Value error, Enter one email address, such as user@example.com, "
            "without a name."
        ),
    )
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


def test_the_first_signup_confirmed_gets_the_name(identity: Api) -> None:
    """Pending signups reserve nothing; the later link answers 409.

    Each address is mailed a link: whoever confirms first gets the name.
    """
    client = identity(SIGNUP_ENABLED=True)
    signup(client, username="alice", email="alice@example.com")
    signup(from_address(client, "198.51.100.3"), email="other@example.com")
    signup(
        from_address(client, "198.51.100.4"), username="x", email="ALICE@example.com"
    )
    run_worker()
    first, second = link_to("other@example.com"), link_to("alice@example.com")
    third = link_to("ALICE@example.com")

    assert confirm(client, first).status_code == 204
    late = confirm(client, second)
    same_address = confirm(client, third)

    assert (late.status_code, late.json()["code"]) == (409, "signup_taken")
    assert late.json()["detail"] == (
        "An account already uses this username or this address; sign up again "
        "with another username, or log in."
    )
    assert same_address.status_code == 204  # Not alice's address: other's has it.
    assert usernames() == ["alice", "x"]
    assert pending() == []
    assert confirm(client, second).json()["code"] == "invalid_token"  # Gone.


def test_signup_links_that_do_not_hold_are_refused(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong, forged, expired, or for a signup gone: one ``400 invalid_token``."""
    client = identity(SIGNUP_ENABLED=True)
    signup(client)
    run_worker()
    token = link_to("alice@example.com")
    (stored,) = pending()
    forged = signing.TimestampSigner(salt="domainkit.auth.other").sign(stored["id"].hex)
    not_a_signup = signing.TimestampSigner(salt="domainkit.auth.signup").sign("x")
    unknown = signup_token(uuid4())

    for bad in (token + "x", forged, not_a_signup, unknown, "garbage"):
        response = confirm(client, bad)
        assert (response.status_code, response.json()["code"]) == (
            400,
            "invalid_token",
        )
    assert signup_of(token) == stored["id"]
    assert signup_of(not_a_signup) is None
    run_async(_set_expiry(stored["id"], utc_now() - timedelta(seconds=1)))
    assert confirm(client, token).json()["code"] == "invalid_token"
    run_async(_set_expiry(stored["id"], utc_now() + timedelta(days=3)))
    now = signing._now()
    monkeypatch.setattr(signing, "_now", lambda: now + 3 * 86400 + 1)
    assert confirm(client, token).json()["code"] == "invalid_token"
    monkeypatch.undo()
    assert len(audited("auth.signup")) == 7
    assert usernames() == []
    assert confirm(client, token).status_code == 204


async def _set_expiry(signup_id: Any, when: Any) -> None:  # noqa: ANN401 - row values.
    """Make a pending signup expire at ``when``."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(pending_signups)
            .where(pending_signups.c.id == signup_id)
            .values(expires_at=when)
        )


def test_two_uses_of_one_link_make_one_account(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link another request already used (its row gone) is refused."""
    client = identity(SIGNUP_ENABLED=True)
    signup(client)
    run_worker()
    token = link_to("alice@example.com")
    (stored,) = pending()
    snapshot = PendingSignup(**stored)
    run_async(_remove(stored["id"]))

    async def read_late(self: SignupStore, signup_id: Any) -> PendingSignup:  # noqa: ANN401
        return snapshot  # Read before the other request deleted it.

    monkeypatch.setattr(SignupStore, "get", read_late)
    response = confirm(client, token)

    assert response.json()["code"] == "invalid_token"
    assert usernames() == []


async def _remove(signup_id: Any) -> None:  # noqa: ANN401 - a row value.
    """Delete a pending signup, as another request using its link would."""
    async with AuthUnitOfWork() as uow:
        await uow.signups.remove(signup_id)
        await uow.commit()


def test_the_worker_skips_signups_gone_or_expired(identity: Api) -> None:
    """Nothing is mailed for a pending signup deleted or expired meanwhile."""
    client = identity(SIGNUP_ENABLED=True)
    signup(client)
    (stored,) = pending()
    run_async(_set_expiry(stored["id"], utc_now()))

    run_async(send_signup_link(stored["id"]))
    run_async(send_signup_link(uuid4()))

    assert mail.outbox == []
    assert confirm(client, signup_token(stored["id"])).json()["code"] == (
        "invalid_token"
    )


def test_a_taken_address_is_told_even_after_a_failed_delivery(
    identity: Api,
) -> None:
    """The pending signup goes once the mail went, so a retry still tells."""
    create_user("alice", email="alice@example.com")
    client = identity(SIGNUP_ENABLED=True)
    signup(client, username="x")
    (stored,) = pending()

    with (
        override_settings(MAIL={"BACKEND": "domainkit_tests.test_auth_signup.Nope"}),
        pytest.raises(ImproperlyConfigured),
    ):
        run_async(send_signup_link(stored["id"]))
    assert len(pending()) == 1
    run_async(send_signup_link(stored["id"]))

    assert mails() == [(EXISTS, "alice@example.com")]
    assert pending() == []


def test_links_point_to_the_signup_page(identity: Api) -> None:
    """``AUTH.SIGNUP_URL`` takes the token, quoted, where it says."""
    client = identity(
        SIGNUP_ENABLED=True,
        SIGNUP_URL="https://app.example.com/welcome?token={token}",
        EMAIL_VERIFICATION_TIMEOUT=timedelta(hours=1),
    )
    signup(client)
    run_worker()

    (message,) = mail.outbox
    (link,) = [line for line in message.body.splitlines() if line.startswith("https")]
    prefix, _, token = link.partition("token=")
    assert prefix == "https://app.example.com/welcome?"
    assert "%3A" in token
    assert "within 1 hour" in message.body
    assert (pending()[0]["expires_at"] - pending()[0]["created_at"]) == timedelta(
        hours=1
    )
    assert confirm(client, unquote(token)).status_code == 204


def test_expired_signups_are_purged_hourly(identity: Api) -> None:
    """With expired tokens: the hashes go once their link no longer works."""
    client = identity(SIGNUP_ENABLED=True)
    signup(client)
    signup(client, username="bob", email="bob@example.com")
    first, second = sorted(pending(), key=lambda row: row["username"])
    run_async(_set_expiry(first["id"], utc_now()))

    # Imported here: importing at collection would register the task early.
    from domainkit.contrib.auth.tasks import clear_expired_tokens

    run_async(clear_expired_tokens())

    assert [row["username"] for row in pending()] == ["bob"]
    assert pending_signups.info == {"domain": "auth", "fixtures": False}
    assert repr(PendingSignup(**second)) == f"PendingSignup(id={second['id']!r})"
    # A link expires at its moment, not after it.
    bob = PendingSignup(**second)
    assert bob.expired(bob.expires_at)
    assert not bob.expired(bob.expires_at - timedelta(microseconds=1))
    assert run_async(_clear(bob.expires_at - timedelta(microseconds=1))) == 0
    assert run_async(_clear(bob.expires_at)) == 1
    assert pending() == []


async def _clear(now: Any) -> int:  # noqa: ANN401 - a moment.
    """Purge the pending signups expired at ``now``."""
    async with AuthUnitOfWork() as uow:
        count = await uow.signups.clear_expired(now)
        await uow.commit()
    return count


def test_confirming_is_off_with_signups(identity: Api) -> None:
    """``/signup/confirm`` answers 404 too while signing up is off."""
    response = confirm(identity(), signup_token(uuid4()))

    assert (response.status_code, response.json()["code"]) == (404, "not_found")
