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

import pytest
from fastapi.testclient import TestClient

from domainkit import clock
from domainkit.contrib.auth.integration import linked_action_authorization, utc_now
from domainkit.contrib.ciba import (
    CibaUnitOfWork,
    Requester,
    SignInUnknown,
    approve,
    poll,
    start_push,
)
from domainkit.contrib.ciba import api as ciba_api
from domainkit.contrib.ciba import cancel as ciba_cancel
from domainkit.contrib.ciba.services import ciba_settings
from domainkit.contrib.ciba.stores import SignInRequestStore
from domainkit.management.base import run_async
from domainkit_tests.support.auth import audited
from domainkit_tests.test_action_authorisations import rows as authorization_rows
from domainkit_tests.test_ciba import (
    PHONE,
    App,
    Phone,
    app,
    redeem,
    rows,
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


def test_a_page_withdraws_its_unscanned_qr_sign_in(app: App) -> None:
    """Unscanned: its code claims nothing more, and the page polls it expired."""
    web = app()
    phone = Phone(app(PHONE))
    started = web.post("/auth/app-sign-in/qr").json()

    withdrawn = cancel(web, started["request"])
    again = cancel(web, started["request"])
    late = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": started["qr"]}
    )
    polled = redeem(web, started["request"])

    assert (withdrawn.status_code, again.status_code) == (204, 204)
    assert code_of(late) == (422, "qr_code_expired")
    assert code_of(polled) == (400, "expired_token")
    assert audited("ciba.sign_in_denied") == []


def test_a_scanned_qr_sign_in_stays_for_the_user_to_report(app: App) -> None:
    """A relaying page cannot take back the code its victim scanned.

    Once a phone claimed it, the request is that phone's to answer: the
    page's cancel leaves it, and the user can still report it.
    """
    web = app()
    phone = Phone(app(PHONE))
    started = web.post("/auth/app-sign-in/qr").json()
    listed = scan(phone, started["qr"])

    withdrawn = cancel(web, started["request"])
    still = phone.waiting()
    reported = phone.deny(listed["id"], report=True)

    assert withdrawn.status_code == 204, withdrawn.json()
    assert [each["id"] for each in still] == [listed["id"]]
    assert reported.status_code == 204, reported.json()
    assert len(audited("ciba.sign_in_reported")) == 1
    assert code_of(redeem(web, started["request"])) == (400, "access_denied")


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


def test_a_poll_redeems_nothing_once_the_request_ended_meanwhile(app: App) -> None:
    """The redeem reads the time when it writes, not when the poll began."""
    app()
    Phone(app(PHONE))
    original = SignInRequestStore.by_handle

    async def scenario() -> str:
        async with CibaUnitOfWork() as uow:
            user = await uow.users.find_by_identifier("alice")
            assert user is not None
            started = await start_push(uow, user, requester=Requester(None, None))
            [device] = await uow.devices.of_user(user.id)
            assert await approve(uow, user.id, started.request_id, device.id)
            await uow.commit()
        with clock.freeze_time() as frozen:

            async def read_then_expire(self: Any, digest: str) -> Any:  # noqa: ANN401
                found = await original(self, digest)
                frozen.tick(timedelta(minutes=3))  # Past REQUEST_TTL, mid-poll.
                return found

            SignInRequestStore.by_handle = read_then_expire  # type: ignore[method-assign]
            try:
                await poll(started.handle, client_id=None)
            except SignInUnknown as refused:
                return refused.code
            finally:
                SignInRequestStore.by_handle = original  # type: ignore[method-assign]
        return "redeemed"

    assert run_async(scenario()) == "sign_in_unknown"
    assert rows()[0]["redeemed_at"] is None


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


@pytest.mark.parametrize("rate", ["10/minute", None])
def test_a_session_that_asks_and_withdraws_in_a_loop_is_stopped(
    acting: App, rate: str | None
) -> None:
    """Twice ``MAX_PENDING`` unanswered asks in ``REQUEST_TTL``, withdrawn or not.

    A plain password session could push the phone at ``START_RATE``, or
    without end with ``None``, each push gone before it could be
    reported. Closing a dialog or two still asks again at once.
    """
    Phone(acting(PHONE))
    web = acting(START_RATE=rate)
    session = Session(web)
    asked = session.asked()

    answers = []
    for _ in range(8):
        started = session.ask_phone(asked["id"])
        answers.append(started.status_code)
        if started.status_code == 200:
            handle = started.json()["request"]
            cancel(web, handle, "authorizations/cancel", session.headers)
    stopped = session.ask_phone(asked["id"])
    pushed = len(sign_in_pushes())
    with later(121):  # REQUEST_TTL on: the asks of then count no more.
        after = session.ask_phone(asked["id"])

    assert answers == [200] * 6 + [409, 409]
    assert code_of(stopped) == (409, "phone_unavailable")
    assert pushed == 6
    assert after.status_code == 200, after.json()


def test_the_user_reports_an_ask_its_session_withdrew(acting: App) -> None:
    """Not me, from the push: reported, and no push reaches them for the cooldown.

    The push names the request; the phone no longer lists it once
    withdrawn, but reports it until ``REQUEST_TTL`` after it was asked.
    """
    phone = Phone(acting(PHONE))
    web = acting()
    session = Session(web)
    asked = session.asked()
    started = session.ask_phone(asked["id"]).json()
    pushed = sign_in_pushes()[-1]["data"]["request_id"]
    cancel(web, started["request"], "authorizations/cancel", session.headers)
    second = session.ask_phone(asked["id"]).json()
    cancel(web, second["request"], "authorizations/cancel", session.headers)

    listed = phone.waiting()
    reported = phone.deny(pushed, report=True)
    twice = phone.deny(pushed, report=True)
    again = session.ask_phone(asked["id"])
    with later(121):
        late = phone.deny(sign_in_pushes()[-1]["data"]["request_id"], report=True)

    assert listed == []
    assert reported.status_code == 204, reported.json()
    assert code_of(twice) == (404, "sign_in_not_found")  # Answered now.
    assert len(audited("ciba.sign_in_reported")) == 1
    assert code_of(again) == (409, "phone_unavailable")  # REPORT_COOLDOWN.
    assert code_of(late) == (404, "sign_in_not_found")


def test_the_app_lists_a_withdrawn_ask_to_report_it_without_its_push(
    acting: App,
) -> None:
    """``?withdrawn=true``: listed, marked, reportable; never approved nor denied.

    Before 2.7 a withdrawn ask left the list at once, and an app opened
    without its push could not report it. The plain list stays as it was,
    for apps that do not know the mark. Past ``REQUEST_TTL`` after it was
    asked, it is listed no more.
    """
    phone = Phone(acting(PHONE))
    web = acting(NUMBER_MATCHING=False)
    session = Session(web)
    asked, started, _ = asked_of_the_phone(session, phone)
    cancel(web, started["request"], "authorizations/cancel", session.headers)
    second = session.ask_phone(session.asked("Bo")["id"]).json()
    cancel(web, second["request"], "authorizations/cancel", session.headers)
    live = session.ask_phone(session.asked("Cy")["id"])

    plain = phone.waiting()
    listed = phone.send("GET", "/auth/app-sign-in/requests?withdrawn=true").json()
    first = listed[0]
    approved = phone.approve(first["id"], digest=asked_digest(first))
    denied = phone.deny(first["id"])
    reported = phone.deny(first["id"], report=True)
    with later(121):
        gone = phone.send("GET", "/auth/app-sign-in/requests?withdrawn=true").json()

    assert live.status_code == 200, live.json()
    assert [each["withdrawn"] for each in plain] == [False]
    assert [each["withdrawn"] for each in listed] == [True, True, False]
    assert plain[0]["id"] == listed[2]["id"]
    assert first["authorization"]["id"] == asked["id"]  # The act, as auth keeps it.
    assert code_of(approved) == (404, "sign_in_not_found")
    assert code_of(denied) == (404, "sign_in_not_found")
    assert reported.status_code == 204, reported.json()
    assert len(audited("ciba.sign_in_reported")) == 1
    assert gone == []  # The second, unreported: past REQUEST_TTL.


def asked_digest(listed: dict[str, Any]) -> str:
    """The digest of the act ``listed`` shows."""
    digest: str = listed["authorization"]["digest"]
    return digest


def test_an_act_withdrawn_while_the_phone_approves_it_is_not_confirmed(
    acting: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withdrawn after the phone read the act, before it approved: neither happens.

    The approval and auth's confirmation are one transaction: the request
    ended, so nothing is confirmed, and the act can be asked again.
    """
    phone = Phone(acting(PHONE))
    web = acting(NUMBER_MATCHING=False)
    session = Session(web)
    asked, started, listed = asked_of_the_phone(session, phone)
    original = linked_action_authorization

    async def read_then_withdrawn(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        found = await original(*args, **kwargs)
        await ciba_cancel(
            started["request"],
            client_id=None,
            kind="authorization",
            credential_digest=kwargs["login"],
        )
        return found

    monkeypatch.setattr(ciba_api, "linked_action_authorization", read_then_withdrawn)
    approved = phone.approve(listed["id"], digest=listed["authorization"]["digest"])
    monkeypatch.undo()
    again = session.ask_phone(asked["id"])

    assert code_of(approved) == (404, "sign_in_not_found")
    [row] = authorization_rows()
    assert (row["confirmed_at"], row["signature"]) == (None, None)
    assert again.status_code == 200, again.json()


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
