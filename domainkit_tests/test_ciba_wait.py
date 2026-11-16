"""Pages wait for the phone instead of polling (user report L2).

A page polled every ``interval`` seconds: it heard of an approval up to
five seconds late, and a poll too soon slowed it down for the rest of the
request. ``…/wait`` holds until the phone answers, never counts as a
poll, and never redeems.
"""

import threading
import time
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from domainkit import clock
from domainkit_tests.test_auth_cookies import TRUSTED
from domainkit_tests.test_ciba import PHONE, App, Phone, app, redeem, rows, start_push
from domainkit_tests.test_ciba_authorizations import (
    Session,
    acting,
    approve,
    asked_of_the_phone,
)
from domainkit_tests.test_ciba_step_up import FREE_TEXT_STEP_UP

__all__ = ["acting", "app"]  # The fixtures the tests take.

ACT = "authorizations/wait"


def wait(web: TestClient, handle: str, route: str = "wait", **headers: str) -> Any:  # noqa: ANN401
    """Wait for the request ``handle`` names: the response."""
    return web.post(
        f"/auth/app-sign-in/{route}",
        json={"request": handle},
        headers={**TRUSTED, **headers},
    )


def test_a_wait_answers_once_the_phone_approves(app: App) -> None:
    """Pending at the end of a short wait; approved at once after; redeemable."""
    web = app(NUMBER_MATCHING=False, WAIT_SECONDS=1)
    phone = Phone(app(PHONE))
    started = start_push(web)

    begun = time.monotonic()
    pending = wait(web, started["request"])
    assert pending.status_code == 200, pending.json()
    assert pending.json() == {"status": "pending"}
    assert pending.headers["Cache-Control"] == "no-store"
    assert time.monotonic() - begun >= 0.9  # It held the request.
    assert rows()[0]["last_polled_at"] is None  # Not a poll: no interval to keep.

    [waiting] = phone.waiting()
    assert phone.approve(waiting["id"]).status_code == 204
    assert wait(web, started["request"]).json() == {"status": "approved"}
    assert wait(web, started["request"]).json() == {"status": "approved"}  # Kept.
    assert redeem(web, started["request"]).status_code == 200  # No slow_down.
    assert wait(web, started["request"]).json()["code"] == "sign_in_unknown"


def test_a_wait_answers_as_a_poll_once_an_approved_request_expired(app: App) -> None:
    """Approved but never redeemed in time: ``expired_token``, as the poll says.

    A wait that said ``approved`` sent the page to a redeem that answers
    ``expired_token``.
    """
    web = app(NUMBER_MATCHING=False, WAIT_SECONDS=1)
    phone = Phone(app(PHONE))
    started = start_push(web)
    [waiting] = phone.waiting()
    assert phone.approve(waiting["id"]).status_code == 204

    with clock.freeze_time(rows()[0]["expires_at"] + timedelta(seconds=1)):
        waited = wait(web, started["request"])
        polled = redeem(web, started["request"])

    assert waited.json()["code"] == polled.json()["code"] == "expired_token"


def test_a_wait_in_progress_hears_the_approval(app: App) -> None:
    """The phone approves while the page waits: the wait answers then."""
    web = app(NUMBER_MATCHING=False, WAIT_SECONDS=20)
    phone = Phone(app(PHONE))
    started = start_push(web)
    [waiting] = phone.waiting()
    answers: list[Any] = []
    holding = threading.Thread(
        target=lambda: answers.append(wait(web, started["request"]))
    )

    begun = time.monotonic()
    holding.start()
    time.sleep(0.3)
    assert phone.approve(waiting["id"]).status_code == 204
    holding.join(10)

    assert [answer.json() for answer in answers] == [{"status": "approved"}]
    assert time.monotonic() - begun < 5  # Not the whole WAIT_SECONDS.


def test_a_denied_or_unknown_request_answers_as_a_poll_does(app: App) -> None:
    """``access_denied`` and ``sign_in_unknown``, at once."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    started = start_push(web)
    [waiting] = phone.waiting()
    assert phone.deny(waiting["id"]).status_code == 204

    denied = wait(web, started["request"])
    assert (denied.status_code, denied.json()["code"]) == (400, "access_denied")
    assert wait(web, "not-a-handle").json()["code"] == "sign_in_unknown"


@FREE_TEXT_STEP_UP  # The free-text step-up is deprecated (ADR 0144).
def test_a_step_up_waits_for_the_session_that_asked_only(app: App) -> None:
    """Another session cannot wait on its handle; the one that asked can."""
    web = app(NUMBER_MATCHING=False, WAIT_SECONDS=1)
    phone = Phone(app(PHONE))
    asked = web.post(
        "/auth/app-sign-in/step-up",
        json={"message": "Change the payee"},
        headers=phone.headers,
    )
    assert asked.status_code == 200, asked.json()
    handle = asked.json()["request"]

    mine = wait(web, handle, "step-up/wait", **phone.headers)
    assert mine.json() == {"status": "pending"}
    other = Phone(app(PHONE), "bob")
    theirs = wait(web, handle, "step-up/wait", **other.headers)
    assert theirs.json()["code"] == "sign_in_unknown"
    anonymous = wait(web, handle, "step-up/wait")
    assert anonymous.status_code == 401


# An act the phone confirms (ADR 0144): each back-office change asks one.


def test_an_act_waits_until_the_phone_confirms_and_the_token_hands_it_over(
    acting: App,
) -> None:
    """Pending at the end; approved at once once confirmed, and kept; redeemed once.

    The wait never redeems: the token route hands the authorization over,
    and the act runs with it.
    """
    phone = Phone(acting(PHONE))
    session = Session(acting(WAIT_SECONDS=1))
    _, started, listed = asked_of_the_phone(session, phone)
    handle = started["request"]

    begun = time.monotonic()
    pending = wait(session.client, handle, ACT, **session.headers)
    held = time.monotonic() - begun
    polled_at = rows()[0]["last_polled_at"]
    confirmed = approve(phone, listed, started)
    approved = wait(session.client, handle, ACT, **session.headers)
    again = wait(session.client, handle, ACT, **session.headers)
    redeemed = session.redeem(handle)
    renamed = session.rename("Al", redeemed.json()["authorization"])
    spent = wait(session.client, handle, ACT, **session.headers)

    assert pending.status_code == 200, pending.json()
    assert pending.json() == {"status": "pending"}
    assert pending.headers["Cache-Control"] == "no-store"
    assert held >= 0.9  # It held the request.
    assert polled_at is None  # Not a poll: no interval to keep.
    assert confirmed.status_code == 204, confirmed.json()
    assert approved.json() == {"status": "approved"}
    assert approved.headers["Cache-Control"] == "no-store"
    assert again.json() == {"status": "approved"}  # Kept: nothing redeemed.
    assert redeemed.status_code == 200, redeemed.json()  # No slow_down.
    assert renamed.status_code == 200, renamed.json()
    assert spent.json()["code"] == "sign_in_unknown"


def test_an_acts_wait_in_progress_hears_the_phone_confirm(acting: App) -> None:
    """The phone confirms while the page waits: the wait answers then."""
    phone = Phone(acting(PHONE))
    session = Session(acting(WAIT_SECONDS=20))
    _, started, listed = asked_of_the_phone(session, phone)
    answers: list[Any] = []
    holding = threading.Thread(
        target=lambda: answers.append(
            wait(session.client, started["request"], ACT, **session.headers)
        )
    )

    begun = time.monotonic()
    holding.start()
    time.sleep(0.3)
    confirmed = approve(phone, listed, started)
    holding.join(10)

    assert confirmed.status_code == 204, confirmed.json()
    assert [answer.json() for answer in answers] == [{"status": "approved"}]
    assert time.monotonic() - begun < 5  # Not the whole WAIT_SECONDS.


def test_an_acts_wait_is_the_asking_sessions_only(acting: App) -> None:
    """Another login of the user: ``sign_in_unknown``; nobody signed in: 401.

    The handle waits as an act only: never as a login, nor a transaction.
    """
    phone = Phone(acting(PHONE))
    web = acting(WAIT_SECONDS=1)
    session = Session(web)
    other = Session(web)
    _, started, _ = asked_of_the_phone(session, phone)
    handle = started["request"]

    mine = wait(web, handle, ACT, **session.headers)
    theirs = wait(web, handle, ACT, **other.headers)
    anonymous = wait(web, handle, ACT)
    as_login = wait(web, handle)
    as_transaction = wait(web, handle, "transactions/wait", **session.headers)

    assert mine.json() == {"status": "pending"}
    assert theirs.json()["code"] == "sign_in_unknown"
    assert anonymous.status_code == 401
    assert as_login.json()["code"] == "sign_in_unknown"
    assert as_transaction.json()["code"] == "sign_in_unknown"


def test_an_act_the_phone_denies_answers_as_a_poll_does(acting: App) -> None:
    """Another digest denies the request: the wait answers ``access_denied``."""
    phone = Phone(acting(PHONE))
    session = Session(acting())
    _, started, listed = asked_of_the_phone(session, phone)

    changed = phone.approve(
        listed["id"], match_code=started["match_code"], digest="0" * 64
    )
    denied = wait(session.client, started["request"], ACT, **session.headers)

    assert changed.status_code == 409, changed.json()
    assert (denied.status_code, denied.json()["code"]) == (400, "access_denied")
