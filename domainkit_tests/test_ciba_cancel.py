"""The page withdraws what it asked the phone (user report A16).

A page could not take back a request: closing its dialog left the act on
the phone, counted against ``MAX_PENDING`` until it expired, and the only
way to clear it, the phone's "Not me", reported it and stopped the user's
pushes for ``REPORT_COOLDOWN``. ``…/cancel`` ends an unanswered request
now, for the initiator alone, and reports nothing. A push sign-in, which
anyone who knows an identifier starts, is never withdrawn: it stays
counted and reportable until it expires (ADR 0128's push-bombing bounds).
"""

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from domainkit.contrib.auth.integration import utc_now
from domainkit.contrib.ciba import CibaUnitOfWork, Requester, approve, start_push
from domainkit.contrib.ciba.services import ciba_settings
from domainkit.management.base import run_async
from domainkit_tests.support.auth import audited
from domainkit_tests.test_ciba import (
    PHONE,
    App,
    Phone,
    app,
    redeem,
    sign_in_pushes,
)
from domainkit_tests.test_ciba import start_push as push_from
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


def scan(phone: Phone, qr: str) -> dict[str, Any]:
    """Claim the QR code ``qr`` from ``phone``: what it lists of the request."""
    claimed = phone.send("POST", "/auth/app-sign-in/requests/claim", {"code": qr})
    assert claimed.status_code == 200, claimed.json()
    shown: dict[str, Any] = claimed.json()
    return shown


def test_a_page_withdraws_its_qr_sign_in(app: App) -> None:
    """Scanned: gone from the phone. Not yet: its code claims nothing. No report."""
    web = app()
    phone = Phone(app(PHONE))
    scanned = web.post("/auth/app-sign-in/qr").json()
    listed = scan(phone, scanned["qr"])
    unscanned = web.post("/auth/app-sign-in/qr").json()

    withdrawn = cancel(web, scanned["request"])
    again = cancel(web, scanned["request"])
    gone = phone.waiting()
    approved = phone.approve(listed["id"])
    polled = redeem(web, scanned["request"])
    unused = cancel(web, unscanned["request"])
    late = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": unscanned["qr"]}
    )

    assert (withdrawn.status_code, again.status_code) == (204, 204)
    assert gone == []
    assert code_of(approved) == (404, "sign_in_not_found")
    assert code_of(polled) == (400, "expired_token")
    assert unused.status_code == 204, unused.json()
    assert code_of(late) == (422, "qr_code_expired")
    assert audited("ciba.sign_in_denied") == []
    assert audited("ciba.sign_in_reported") == []


def test_a_push_sign_in_is_never_withdrawn_so_pushes_stay_bounded(app: App) -> None:
    """Start and cancel in a loop: ``MAX_PENDING`` pushes, each one reportable.

    Whoever knows an identifier starts a push. Were it withdrawn, the
    user's place would free at once: one address could push ``START_RATE``
    a minute, and the pushes would vanish before the user could report
    them. The handle gets ``sign_in_unknown``, a decoy's too.
    """
    web = app(START_RATE="10/minute")
    phone = Phone(app(PHONE))

    cancelled = [cancel(web, push_from(web)["request"]) for _ in range(5)]
    listed = phone.waiting()
    reported = phone.deny(listed[0]["id"], report=True)
    push_from(web)

    assert {code_of(each) for each in cancelled} == {(400, "sign_in_unknown")}
    assert len(sign_in_pushes()) == ciba_settings().MAX_PENDING == 3
    assert len(listed) == 3
    assert reported.status_code == 204, reported.json()
    assert len(audited("ciba.sign_in_reported")) == 1
    assert len(sign_in_pushes()) == 3  # REPORT_COOLDOWN: none since.


def test_a_page_cancels_only_its_own_unanswered_sign_ins(app: App) -> None:
    """Unknown or redeemed: ``sign_in_unknown``; approved: left to be redeemed."""
    web = app()
    phone = Phone(app(PHONE))
    redeemed = web.post("/auth/app-sign-in/qr").json()
    approved = web.post("/auth/app-sign-in/qr").json()
    for started in (redeemed, approved):
        assert phone.approve(scan(phone, started["qr"])["id"]).status_code == 204
    with later():
        assert redeem(web, redeemed["request"]).status_code == 200

    unknown = cancel(web, "not-a-handle")
    spent = cancel(web, redeemed["request"])
    answered = cancel(web, approved["request"])
    with later(12):
        polled = redeem(web, approved["request"])

    assert code_of(unknown) == (400, "sign_in_unknown")
    assert code_of(spent) == (400, "sign_in_unknown")
    assert answered.status_code == 204, answered.json()
    assert polled.status_code == 200, polled.json()  # The phone answered first.


def test_an_act_the_phone_approved_is_not_withdrawn(acting: App) -> None:
    """Approved, then the page cancels: the authorization is still redeemed.

    Ending it would leave auth's authorization confirmed and unusable:
    asking again is ``409 authorization_confirmed``.
    """
    phone = Phone(acting(PHONE))
    web = acting(NUMBER_MATCHING=False)
    session = Session(web)
    asked, started, listed = asked_of_the_phone(session, phone)
    approved = phone.approve(listed["id"], digest=listed["authorization"]["digest"])

    answered = cancel(web, started["request"], "authorizations/cancel", session.headers)
    with later():
        redeemed = session.redeem(started["request"])

    assert approved.status_code == 204, approved.json()
    assert answered.status_code == 204, answered.json()
    assert redeemed.status_code == 200, redeemed.json()
    assert redeemed.json()["authorization"].startswith(f"{asked['id']}.")
    assert session.rename("Al", redeemed.json()["authorization"]).status_code == 200


def test_an_ended_request_is_never_redeemed(app: App) -> None:
    """A poll that read it approved redeems nothing once it has ended meanwhile."""
    app()
    Phone(app(PHONE))

    async def scenario() -> tuple[bool, bool]:
        async with CibaUnitOfWork() as uow:
            user = await uow.users.find_by_identifier("alice")
            assert user is not None
            started = await start_push(uow, user, requester=Requester(None, None))
            [device] = await uow.devices.of_user(user.id)
            assert await approve(uow, user.id, started.request_id, device.id)
            await uow.commit()
        async with CibaUnitOfWork() as uow:
            request = await uow.requests.get(started.request_id)
            assert request is not None
            after = request.expires_at + timedelta(seconds=1)
            late = await uow.requests.redeem(request.id, after)
            in_time = await uow.requests.redeem(request.id, utc_now())
            await uow.commit()
        return late, in_time

    assert run_async(scenario()) == (False, True)


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
