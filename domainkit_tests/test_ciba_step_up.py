"""A signed-in web session asks the phone: a step-up, or a transaction (ADR 0129)."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from domainkit.conf import override_settings
from domainkit.contrib.auth.authentication import Credentials
from domainkit.contrib.auth.integration import (
    ReauthenticationRequired,
    auth_settings,
    confirm_transaction_with,
    sca_transaction,
    utc_now,
)
from domainkit.contrib.auth.sca import consume_authorization, request_authorization
from domainkit.contrib.auth.tables import sca_challenges
from domainkit.contrib.ciba.api import _session_of
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit.money import Money
from domainkit_tests.test_auth_http import bearer, create_user, login
from domainkit_tests.test_ciba import PHONE, App, Phone, app
from domainkit_tests.test_devices_edges import http

__all__ = ["app"]  # The fixture the tests take.

IBAN = "DE89370400440532013000"
MESSAGE = "Add payee DE89 3704 0044"


def web_of(client: TestClient, username: str = "alice") -> dict[str, str]:
    """A web session of ``username``: its headers."""
    return bearer(login(client, username)["access_token"])


def ask(client: TestClient, headers: dict[str, str], message: str = MESSAGE) -> Any:  # noqa: ANN401
    """``POST /app-sign-in/step-up``: the response."""
    return client.post(
        "/auth/app-sign-in/step-up", json={"message": message}, headers=headers
    )


def redeem(
    client: TestClient, headers: dict[str, str], handle: str, what: str = "step-up"
) -> Any:  # noqa: ANN401 - a response.
    """``POST /app-sign-in/{what}/token``: the response."""
    return client.post(
        f"/auth/app-sign-in/{what}/token", json={"request": handle}, headers=headers
    )


def approve_last(phone: Phone, match_code: str | None) -> dict[str, Any]:
    """Approve what the phone lists last; what it was shown."""
    shown = phone.waiting()[-1]
    approved = phone.approve(shown["id"], match_code=match_code)
    assert approved.status_code == 204, approved.json()
    return shown


def transaction_of(user_id: UUID, amount: str = "250.00") -> UUID:
    """A transfer of ``user_id`` waiting to be confirmed: its id."""
    challenge = run_async(
        request_authorization(
            user_id, Money.of(amount, "EUR"), IBAN, purpose="transfer"
        )
    )
    return challenge.id


def change_transaction(challenge_id: UUID, *, gone: bool = False) -> None:
    """Expire the transaction, or delete it."""

    async def change() -> None:
        async with connections.engine().begin() as connection:
            where = sca_challenges.c.id == challenge_id
            if gone:
                await connection.execute(delete(sca_challenges).where(where))
            else:
                expired = utc_now() - timedelta(seconds=1)
                await connection.execute(
                    update(sca_challenges).where(where).values(expires_at=expired)
                )

    run_async(change())


def test_the_phone_steps_the_web_session_up(app: App) -> None:
    """Asked, shown the message, approved with the number: the session holds hwk."""
    phone = Phone(app(PHONE))
    web = app()
    headers = web_of(web)
    before = web.post("/payees", headers=headers)

    unanswered = ask(web, headers).json()["request"]
    pending = redeem(web, headers, unanswered)
    started = ask(web, headers)
    shown = approve_last(phone, started.json()["match_code"])
    stepped = redeem(web, headers, started.json()["request"])
    after = web.post("/payees", headers=headers)
    again = redeem(web, headers, started.json()["request"])

    assert before.status_code == 401
    assert started.status_code == 200, started.json()
    assert started.headers["Cache-Control"] == "no-store"
    assert pending.json()["code"] == "authorization_pending"
    assert (shown["kind"], shown["binding_message"]) == ("step_up", MESSAGE)
    assert shown["transaction"] is None
    assert stepped.status_code == 200, stepped.json()
    assert stepped.json()["amr"] == ["hwk"]
    assert after.status_code == 204
    assert again.json()["code"] == "sign_in_unknown"  # Once.


def test_a_step_up_is_only_its_sessions_and_never_a_login(app: App) -> None:
    """Another session, the sign-in routes, and the transaction route: unknown."""
    phone = Phone(app(PHONE))
    web = app()
    headers = web_of(web)
    started = ask(web, headers).json()
    approve_last(phone, started["match_code"])

    other = redeem(web, web_of(web), started["request"])
    as_login = web.post("/auth/app-sign-in/token", json={"request": started["request"]})
    as_transaction = redeem(web, headers, started["request"], "transactions")
    still = redeem(web, headers, started["request"])

    for refused in (other, as_login, as_transaction):
        assert refused.json()["code"] == "sign_in_unknown"
    assert still.status_code == 200, still.json()  # None of those spent it.


def test_a_step_up_needs_a_phone_to_ask(app: App) -> None:
    """No bound device: 409; a message the phone cannot show whole: 422."""
    web = app()
    create_user("bob")
    headers = web_of(web, "bob")

    unbound = ask(web, headers)
    long = ask(web, headers, "x" * 65)
    control = ask(web, headers, "Pay‮evil")
    blank = ask(web, headers, "   ")
    anonymous = web.post("/auth/app-sign-in/step-up", json={"message": MESSAGE})

    assert (unbound.status_code, unbound.json()["code"]) == (409, "phone_unavailable")
    for refused in (long, control, blank):
        assert refused.status_code == 422
    assert anonymous.status_code == 401


def test_staff_acting_as_the_user_ask_no_phone() -> None:
    """An impersonation never asks the user's phone."""
    user: Any = object()
    request = http(auth=Credentials(user, "token", "digest", impersonator=uuid4()))

    with pytest.raises(ReauthenticationRequired):
        _session_of(request)


def test_sessions_are_told_apart_by_their_credentials() -> None:
    """Two tokens, or two logins of JSON Web Tokens: two digests."""
    user: Any = object()
    login_id = uuid4()

    def digest(credentials: Credentials) -> str:
        return _session_of(http(auth=credentials))[1]

    tokens = {
        digest(Credentials(user, "token", "one")),
        digest(Credentials(user, "token", "two")),
        digest(Credentials(user, "jwt", login=login_id)),
        digest(Credentials(user, "jwt", login=uuid4())),
    }
    assert len(tokens) == 4
    assert digest(Credentials(user, "jwt", login=login_id)) in tokens


def test_the_phone_confirms_a_transaction(app: App) -> None:
    """Shown the stored amount and payee; the authorization spends once."""
    phone = Phone(app(PHONE))
    web = app()
    headers = web_of(web)
    challenge_id = transaction_of(phone.user_id)

    started = web.post(
        f"/auth/app-sign-in/transactions/{challenge_id}", headers=headers
    )
    shown = approve_last(phone, started.json()["match_code"])
    answered = redeem(web, headers, started.json()["request"], "transactions")
    authorization = answered.json()["authorization"]
    spent = run_async(
        consume_authorization(
            authorization,
            user_id=phone.user_id,
            amount=Money.of("250.00", "EUR"),
            payee=IBAN,
            purpose="transfer",
        )
    )

    assert started.status_code == 200, started.json()
    assert shown["kind"] == "transaction"
    assert shown["transaction"] == {
        "amount": {"amount": "250.00", "currency": "EUR"},
        "payee": IBAN,
        "purpose": "transfer",
    }
    assert answered.status_code == 200, answered.json()
    assert authorization.startswith(f"{challenge_id}.")
    assert spent.amr == "hwk"


def test_transactions_the_phone_cannot_confirm(app: App) -> None:
    """Another's or none: 404; closed: 409; the password asked too: 409."""
    phone = Phone(app(PHONE))
    web = app()
    headers = web_of(web)
    bob = create_user("bob")
    theirs = transaction_of(UUID(str(bob)))
    expired = transaction_of(phone.user_id)
    change_transaction(expired)
    confirmed = transaction_of(phone.user_id)
    challenge = run_async(sca_transaction(confirmed, phone.user_id))
    run_async(confirm_transaction_with(challenge, ["hwk"]))
    mine = transaction_of(phone.user_id)

    def start(challenge_id: UUID | str) -> Any:  # noqa: ANN401 - a response.
        return web.post(
            f"/auth/app-sign-in/transactions/{challenge_id}", headers=headers
        )

    sca = auth_settings().SCA.model_dump()
    with override_settings(
        AUTH={
            **auth_settings().model_dump(exclude_unset=True),
            "SCA": {**sca, "METHODS": ("pwd", "otp")},
        }
    ):
        with_password = start(mine)

    assert start(theirs).json()["code"] == "transaction_not_found"
    assert start(uuid4()).json()["code"] == "transaction_not_found"
    assert (start(expired).status_code, start(expired).json()["code"]) == (
        409,
        "transaction_closed",
    )
    assert start(confirmed).json()["code"] == "transaction_closed"
    assert (with_password.status_code, with_password.json()["code"]) == (
        409,
        "transaction_needs_other_factors",
    )


def test_a_transaction_that_changed_while_the_phone_answered(app: App) -> None:
    """Expired, confirmed elsewhere, or gone: the answer says so."""
    phone = Phone(app(PHONE))
    web = app()
    headers = web_of(web)

    def approved() -> tuple[UUID, str]:
        challenge_id = transaction_of(phone.user_id)
        started = web.post(
            f"/auth/app-sign-in/transactions/{challenge_id}", headers=headers
        ).json()
        approve_last(phone, started["match_code"])
        return challenge_id, started["request"]

    expired, late = approved()
    change_transaction(expired)
    confirmed, twice = approved()
    challenge = run_async(sca_transaction(confirmed, phone.user_id))
    run_async(confirm_transaction_with(challenge, ["hwk"]))
    gone, missing = approved()

    change_transaction(gone, gone=True)
    listed = phone.waiting()

    assert redeem(web, headers, late, "transactions").json()["code"] == (
        "transaction_expired"
    )
    assert redeem(web, headers, twice, "transactions").json()["code"] == (
        "transaction_confirmed"
    )
    assert redeem(web, headers, missing, "transactions").json()["code"] == (
        "transaction_not_found"
    )
    assert listed == []  # Answered: nothing waits.


def test_a_transaction_auth_no_longer_has_is_shown_as_none(app: App) -> None:
    """The phone still lists the request; it shows no transaction."""
    phone = Phone(app(PHONE))
    web = app()
    headers = web_of(web)
    challenge_id = transaction_of(phone.user_id)
    web.post(f"/auth/app-sign-in/transactions/{challenge_id}", headers=headers)
    change_transaction(challenge_id, gone=True)

    [shown] = phone.waiting()

    assert (shown["kind"], shown["transaction"]) == ("transaction", None)
