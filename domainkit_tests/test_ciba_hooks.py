"""Hooks of the project's own sign-in pages (user report A8).

A project re-implemented the start and the approval to name its dashboard
and check who may use it: ciba had no hook before an approval, and its QR
start took no message. A refusal left the request waiting until it
expired, the page polling on (user report A17); and a login gate could not
tell which sign-in a login redeemed (user report A11).
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from starlette.requests import Request

from domainkit import clock
from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.gates import (
    LoginOrigin,
    SessionLimits,
    login_origin,
    on_login_gate,
    on_session_limits,
)
from domainkit.contrib.ciba import CibaUnitOfWork, deny
from domainkit.contrib.ciba.hooks import on_approve, on_start
from domainkit.ddd import BusinessRuleViolation, ConcurrentUpdate, PermissionDenied
from domainkit.http.errors import APIError
from domainkit.management.base import run_async
from domainkit_tests.support.auth import audited, bearer, login
from domainkit_tests.test_auth_principals import credentials_of
from domainkit_tests.test_ciba import (
    LAPTOP,
    PHONE,
    App,
    Phone,
    app,
    redeem,
    rows,
    sign_in_pushes,
    start_push,
)
from domainkit_tests.test_ciba_wait import wait

__all__ = ["app"]  # The fixture the tests take.

DASHBOARD = "Sign in to the dashboard"


async def name_the_dashboard(request: Request) -> str | None:
    """The page's own message, shown on both devices."""
    return DASHBOARD if request.headers.get("x-page") == "dashboard" else None


async def nobody_from_the_kiosk(request: Request) -> str | None:
    """Refuse starts from one page."""
    if request.headers.get("x-page") == "kiosk":
        raise PermissionDenied("Sign-ins do not start from the kiosk.")
    return None


async def staff_only(request: Request, user: Any, sign_in: Any) -> None:  # noqa: ANN401
    """The dashboard's sign-ins are for staff."""
    if sign_in.binding_message == DASHBOARD and not user.is_staff:
        raise PermissionDenied("Only staff sign in to the dashboard.")


def test_a_start_takes_the_hooks_message_or_their_refusal(app: App) -> None:
    """Push and QR carry the message; a hook that refuses stores nothing."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    on_start(name_the_dashboard, domain="ciba")
    on_start(nobody_from_the_kiosk, domain="ciba")

    pushed = web.post(
        "/auth/app-sign-in/push",
        json={"identifier": "alice"},
        headers={"X-Page": "dashboard"},
    )
    assert pushed.status_code == 200, pushed.json()
    [waiting] = phone.waiting()
    assert waiting["binding_message"] == DASHBOARD
    scanned = web.post("/auth/app-sign-in/qr", headers={"X-Page": "dashboard"})
    assert scanned.status_code == 200, scanned.json()
    assert [row["binding_message"] for row in rows()] == [DASHBOARD, DASHBOARD]

    refused = web.post("/auth/app-sign-in/qr", headers={"X-Page": "kiosk"})
    assert refused.status_code == 403
    assert len(rows()) == 2
    assert start_push(web)["request"]  # No page named: no message, no refusal.
    assert rows()[-1]["binding_message"] is None


def dashboard_push(web: Any) -> dict[str, Any]:  # noqa: ANN401 - a client.
    """Start a push sign-in from the dashboard: what it answers."""
    answer = web.post(
        "/auth/app-sign-in/push",
        json={"identifier": "alice"},
        headers={"X-Page": "dashboard"},
    )
    assert answer.status_code == 200, answer.json()
    started: dict[str, Any] = answer.json()
    return started


def test_an_approval_a_hook_refuses_is_denied_and_reports_nothing(app: App) -> None:
    """The phone is told the refusal; the page hears ``access_denied`` at once.

    Denied as the phone's "Deny" would: not reported, so the user's next
    push is sent (user report A17).
    """
    web = app(NUMBER_MATCHING=False, WAIT_SECONDS=1)
    phone = Phone(app(PHONE))
    on_start(name_the_dashboard, domain="ciba")
    on_approve(staff_only, domain="ciba")
    started = dashboard_push(web)
    [waiting] = phone.waiting()

    refused = phone.approve(waiting["id"])
    waited = wait(web, started["request"])
    polled = redeem(web, started["request"])
    start_push(web)

    assert refused.status_code == 403
    assert refused.json()["detail"] == "Only staff sign in to the dashboard."
    assert waited.json()["code"] == "access_denied"
    assert polled.json()["code"] == "access_denied"
    assert [each["id"] for each in phone.waiting()] != [waiting["id"]]
    assert len(sign_in_pushes()) == 2
    assert len(audited("ciba.sign_in_denied")) == 1
    assert audited("ciba.sign_in_reported") == []
    assert [row["reported"] for row in rows()] == [False, False]


@pytest.mark.parametrize(
    ("error", "status", "denied"),
    [
        (PermissionDenied("Not from the phone."), 403, True),
        (APIError(403, "Not from the phone.", code="not_here"), 403, True),
        (ConcurrentUpdate("The user changed meanwhile."), 409, False),
        (APIError(429, "Too many checks.", code="rate_limited"), 429, False),
        (BusinessRuleViolation("Not now."), 422, False),
        (APIError(503, "The directory is down.", code="directory_down"), 503, False),
    ],
    ids=["denied", "forbidden", "retry", "throttled", "rule", "hooks-own-failure"],
)
def test_a_refusal_denies_and_any_other_error_does_not(
    app: App, error: Exception, status: int, *, denied: bool
) -> None:
    """``PermissionDenied`` or a 403 refuses, and denies; anything else waits.

    A conflict to retry, a throttle, or the hook's own failure is no
    answer: the phone may approve again once it passes.
    """
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    started = start_push(web)
    [waiting] = phone.waiting()

    async def refuse(request: Request, user: Any, sign_in: Any) -> None:  # noqa: ANN401
        raise error

    on_approve(refuse, domain="ciba")
    answer = phone.approve(waiting["id"])
    polled = redeem(web, started["request"]).json()["code"]

    assert answer.status_code == status
    assert polled == ("access_denied" if denied else "authorization_pending")
    assert (rows()[0]["denied_at"] is not None) is denied


def test_a_hook_that_denied_the_request_itself_is_answered_its_refusal(
    app: App,
) -> None:
    """A project's 2.4 workaround, ``deny()`` then raise: still its 403, denied once."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    started = start_push(web)
    [waiting] = phone.waiting()

    async def deny_then_refuse(request: Request, user: Any, sign_in: Any) -> None:  # noqa: ANN401
        async with CibaUnitOfWork() as uow:
            await deny(uow, user.id, sign_in.id)
            await uow.commit()
        raise PermissionDenied("Only staff sign in to the dashboard.")

    on_approve(deny_then_refuse, domain="ciba")
    refused = phone.approve(waiting["id"])

    assert refused.status_code == 403
    assert refused.json()["detail"] == "Only staff sign in to the dashboard."
    assert redeem(web, started["request"]).json()["code"] == "access_denied"
    assert len(audited("ciba.sign_in_denied")) == 1


def test_a_login_gate_and_session_limits_see_the_sign_in_it_redeems(app: App) -> None:
    """The request's id and binding message, for a push and a QR code (user report A11).

    A password login names none, and the login does not keep them:
    ``Credentials.origin`` of the push's login has neither.
    """
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(LAPTOP))
    on_start(name_the_dashboard, domain="ciba")
    gated: list[LoginOrigin | None] = []
    limited: list[LoginOrigin] = []

    async def gate(request: Request, user: AbstractBaseUser) -> None:
        gated.append(login_origin(request))

    async def limits(user: AbstractBaseUser, origin: LoginOrigin) -> SessionLimits:
        limited.append(origin)
        return SessionLimits()

    on_login_gate(gate, domain="ciba")
    on_session_limits(limits, domain="ciba")
    pushed = dashboard_push(web)
    [by_push] = phone.waiting()
    shown = web.post("/auth/app-sign-in/qr").json()
    claimed = phone.send(
        "POST", "/auth/app-sign-in/requests/claim", {"code": shown["qr"]}
    )
    by_qr = claimed.json()
    for each in (by_push, by_qr):
        assert phone.approve(each["id"]).status_code == 204
    with clock.freeze_time(clock.now() + timedelta(seconds=6)):
        tokens = redeem(web, pushed["request"])
        session = redeem(web, shown["request"], "session")
    assert tokens.status_code == 200, tokens.json()
    assert session.status_code == 200, session.json()
    login(app(), "alice")  # A client of its own: no session cookie.
    later = run_async(credentials_of(headers=bearer(tokens.json()["access_token"])))

    seen = [
        (origin.method, origin.sign_in_request_id, origin.binding_message)
        for origin in gated
        if origin is not None
    ]
    assert seen == [
        ("push", UUID(by_push["id"]), "Sign in to the dashboard"),
        ("qr", UUID(by_qr["id"]), None),
        ("password", None, None),
    ]
    assert [
        (origin.sign_in_request_id, origin.binding_message) for origin in limited
    ] == [(sign_in, message) for _, sign_in, message in seen]
    assert later is not None
    assert later.origin is not None
    assert (later.origin.method, later.origin.sign_in_request_id) == ("push", None)


def test_a_message_the_phone_cannot_show_is_a_mistake(app: App) -> None:
    """A hook's message holds 1 to 64 printable characters."""
    web = app()

    async def too_long(request: Request) -> str:
        return "x" * 65

    on_start(too_long, domain="ciba")
    answer = web.post("/auth/app-sign-in/qr")

    assert answer.status_code == 500  # The project's mistake, reported.
    assert rows() == []
