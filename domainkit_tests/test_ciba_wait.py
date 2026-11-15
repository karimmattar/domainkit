"""Pages wait for the phone instead of polling (user report L2).

A page polled every ``interval`` seconds: it heard of an approval up to
five seconds late, and a poll too soon slowed it down for the rest of the
request. ``…/wait`` holds until the phone answers, never counts as a
poll, and never redeems.
"""

import threading
import time
from typing import Any

from fastapi.testclient import TestClient

from domainkit_tests.test_auth_cookies import TRUSTED
from domainkit_tests.test_ciba import PHONE, App, Phone, app, redeem, rows, start_push

__all__ = ["app"]  # The fixture the tests take.


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
