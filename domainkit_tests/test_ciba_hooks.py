"""Hooks of the project's own sign-in pages (user report A8).

A project re-implemented the start and the approval to name its dashboard
and check who may use it: ciba had no hook before an approval, and its QR
start took no message.
"""

from typing import Any

from starlette.requests import Request

from domainkit.contrib.ciba.hooks import on_approve, on_start
from domainkit.ddd import PermissionDenied
from domainkit_tests.test_ciba import PHONE, App, Phone, app, redeem, rows, start_push

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


def test_an_approval_a_hook_refuses_approves_nothing(app: App) -> None:
    """The phone is refused; the page's request still waits."""
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    on_start(name_the_dashboard, domain="ciba")
    on_approve(staff_only, domain="ciba")
    started = web.post(
        "/auth/app-sign-in/push",
        json={"identifier": "alice"},
        headers={"X-Page": "dashboard"},
    ).json()
    [waiting] = phone.waiting()

    refused = phone.approve(waiting["id"])
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Only staff sign in to the dashboard."
    assert redeem(web, started["request"]).json()["code"] == "authorization_pending"
    assert [each["id"] for each in phone.waiting()] == [waiting["id"]]


def test_a_message_the_phone_cannot_show_is_a_mistake(app: App) -> None:
    """A hook's message holds 1 to 64 printable characters."""
    web = app()

    async def too_long(request: Request) -> str:
        return "x" * 65

    on_start(too_long, domain="ciba")
    answer = web.post("/auth/app-sign-in/qr")

    assert answer.status_code == 500  # The project's mistake, reported.
    assert rows() == []
