"""The page withdraws what it asked the phone (user report A16).

A page could not take back a request: closing its dialog left the push on
the phone, counted against ``MAX_PENDING`` until it expired, and the only
way to clear it, the phone's "Not me", reported it and stopped the user's
pushes for ``REPORT_COOLDOWN``. ``…/cancel`` ends the request now, for
the initiator alone, and reports nothing.
"""

from typing import Any

from fastapi.testclient import TestClient

from domainkit_tests.support.auth import audited
from domainkit_tests.test_ciba import (
    PHONE,
    App,
    Phone,
    app,
    redeem,
    rows,
    sign_in_pushes,
    start_push,
)
from domainkit_tests.test_ciba_authorizations import (
    Session,
    acting,
    asked_of_the_phone,
    code_of,
    later,
)
from domainkit_tests.test_ciba_step_up import transaction_of

__all__ = ["acting", "app"]  # The fixtures the tests take.


def cancel(
    web: TestClient,
    handle: str,
    route: str = "cancel",
    headers: dict[str, str] | None = None,
) -> Any:  # noqa: ANN401 - a response.
    """``POST /app-sign-in/{route}`` for the request ``handle`` names."""
    return web.post(
        f"/auth/app-sign-in/{route}", json={"request": handle}, headers=headers or {}
    )


def test_a_page_withdraws_its_sign_in_and_the_phones_place_is_free(app: App) -> None:
    """Gone from the phone, ended for the page, no report; the next push is sent.

    With ``MAX_PENDING`` 1, a second start is a decoy until the first ends;
    once withdrawn, the third reaches the phone at once.
    """
    web = app(MAX_PENDING=1)
    phone = Phone(app(PHONE))
    started = start_push(web)
    [listed] = phone.waiting()
    crowded = start_push(web)  # A decoy: one push waits already.

    cancelled = cancel(web, started["request"])
    again = cancel(web, started["request"])
    decoy = cancel(web, crowded["request"])
    approved = phone.approve(listed["id"], match_code=started["match_code"])
    polled = redeem(web, started["request"])
    after = start_push(web)

    assert cancelled.status_code == 204, cancelled.json()
    assert (again.status_code, decoy.status_code) == (204, 204)  # Answers alike.
    assert code_of(approved) == (404, "sign_in_not_found")
    assert code_of(polled) == (400, "expired_token")
    assert len(sign_in_pushes()) == 2  # The decoy pushed nothing; the third did.
    [waiting] = phone.waiting()
    assert waiting["id"] == sign_in_pushes()[-1]["data"]["request_id"]
    assert after["request"] != started["request"]
    assert audited("ciba.sign_in_denied") == []
    assert audited("ciba.sign_in_reported") == []
    assert [row["reported"] for row in rows()] == [False, False, False]


def test_a_page_cancels_only_its_own_unredeemed_sign_ins(app: App) -> None:
    """Unknown or redeemed: ``sign_in_unknown``; approved but unredeemed: ended."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    redeemed, approved = start_push(web), start_push(web)
    for listed in phone.waiting():
        assert phone.approve(listed["id"]).status_code == 204
    with later():
        assert redeem(web, redeemed["request"]).status_code == 200

    unknown = cancel(web, "not-a-handle")
    spent = cancel(web, redeemed["request"])
    withdrawn = cancel(web, approved["request"])
    with later(12):
        polled = redeem(web, approved["request"])

    assert code_of(unknown) == (400, "sign_in_unknown")
    assert code_of(spent) == (400, "sign_in_unknown")
    assert withdrawn.status_code == 204, withdrawn.json()
    assert code_of(polled) == (400, "expired_token")  # Nobody redeems it now.


def test_only_the_session_that_asked_withdraws_its_act(acting: App) -> None:
    """Another login of the user, another kind, the page: ``sign_in_unknown``.

    Withdrawn, the act is gone from the phone; the authorization stays,
    and the phone can be asked again at once, ``MAX_PENDING`` 1 or not.
    """
    phone = Phone(acting(PHONE))
    web = acting(NUMBER_MATCHING=False, MAX_PENDING=1)
    session, other = Session(web), Session(web)
    asked, started, _ = asked_of_the_phone(session, phone)
    handle = started["request"]

    by_other = cancel(web, handle, "authorizations/cancel", other.headers)
    as_transaction = cancel(web, handle, "transactions/cancel", session.headers)
    as_page = cancel(web, handle)
    unsigned = cancel(web, handle, "authorizations/cancel")
    still = phone.waiting()
    withdrawn = cancel(web, handle, "authorizations/cancel", session.headers)
    gone = phone.waiting()
    polled = session.redeem(handle)
    again = session.ask_phone(asked["id"])
    [listed] = phone.waiting()
    approved = phone.approve(listed["id"], digest=listed["authorization"]["digest"])
    with later():
        answered = session.redeem(again.json()["request"])

    for refused in (by_other, as_transaction, as_page):
        assert code_of(refused) == (400, "sign_in_unknown")
    assert unsigned.status_code == 401
    assert len(still) == 1
    assert withdrawn.status_code == 204, withdrawn.json()
    assert gone == []
    assert code_of(polled) == (400, "expired_token")
    assert again.status_code == 200, again.json()
    assert listed["authorization"]["id"] == asked["id"]
    assert approved.status_code == 204, approved.json()
    assert answered.json()["authorization"].startswith(f"{asked['id']}.")
    assert audited("ciba.sign_in_reported") == []


def test_a_session_withdraws_the_transaction_it_asked_the_phone(acting: App) -> None:
    """Gone from the phone; the transaction still waits to be confirmed."""
    phone = Phone(acting(PHONE))
    web = acting(NUMBER_MATCHING=False)
    session = Session(web)
    challenge = transaction_of(phone.user_id)
    started = web.post(
        f"/auth/app-sign-in/transactions/{challenge}", headers=session.headers
    )
    listed = phone.waiting()

    withdrawn = cancel(
        web, started.json()["request"], "transactions/cancel", session.headers
    )
    again = web.post(
        f"/auth/app-sign-in/transactions/{challenge}", headers=session.headers
    )

    assert started.status_code == 200, started.json()
    assert [each["kind"] for each in listed] == ["transaction"]
    assert withdrawn.status_code == 204, withdrawn.json()
    assert again.status_code == 200, again.json()  # Still open: asked anew.
    [anew] = phone.waiting()
    assert anew["id"] != listed[0]["id"]
