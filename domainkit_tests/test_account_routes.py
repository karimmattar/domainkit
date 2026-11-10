"""Every route that changes an account says who may call it (ADR 0136).

The phone-only check (``on_manage_logins``) was added route by route, and
three releases each found routes the one before missed. A route of the
auth apps that a signed-in user changes something with now declares the
check (``dependencies=[MANAGES_LOGINS]``), or is listed here with why it
needs none. A new route that does neither fails this test.
"""

import importlib
import re
from collections.abc import Callable, Iterator
from typing import Any, Final
from uuid import uuid4

from fastapi import APIRouter
from fastapi.dependencies.models import Dependant

from domainkit.contrib.auth.gates import manages_logins
from domainkit.http.access import public, served_routes
from domainkit_tests.test_ciba import PHONE, App, Phone, app
from domainkit_tests.test_ciba_logins import refused, web_login

__all__ = ["app"]  # The fixture the tests take.

ROUTERS: Final = (
    "auth.api",
    "auth.cookies",
    "auth.mfa",
    "auth.personal_tokens",
    "auth.recovery",
    "auth.sca",
    "auth.signup",
    "auth.stepup",
    "ciba.api",
    "devices.api",
    "identity.api",
    "identity.saml",
    "phones.api",
)
"""The routers users reach their own account through. Staff's routes
(``auth.admin``) act on other users, under model permissions."""

DECIDES: Final = {
    "POST /logout": "only everywhere and others end logins that are not its own",
    "DELETE /tokens/{token_id}": "only a token that is not the request's own",
    "POST /password": "only when asked to end the devices' logins",
}
"""Routes that run the check in their body, for some requests only."""

NO_WAY_IN: Final = {
    "POST /step-up": "proves the user again, for this session",
    "POST /passkeys/step-up/options": "proves the user again, for this session",
    "POST /passkeys/step-up": "proves the user again, for this session",
    "POST /devices/step-up": "signed by the device",
    "POST /app-sign-in/requests/claim": "signed by the device",
    "POST /app-sign-in/requests/{request_id}/approve": "signed by the device",
    "POST /app-sign-in/requests/{request_id}/deny": "signed by the device",
    "POST /app-sign-in/step-up": "asks the phone; the phone answers",
    "POST /app-sign-in/step-up/token": "redeems what the phone approved",
    "POST /app-sign-in/transactions/{challenge_id}": "asks the phone",
    "POST /app-sign-in/transactions/token": "redeems what the phone confirmed",
    "POST /sca/{challenge_id}/confirm": "confirms one transaction, with factors",
    "POST /email-verification": "sends a code to the account's own address",
    "POST /email-verification/code": "proves the account's own address",
    "POST /mobile-verification": "sends a code to the account's own number",
    "POST /mobile-verification/code": "proves the account's own number",
    "POST /phones/codes": "a number the project uses; nobody logs in by it",
    "POST /phones": "a number the project uses; nobody logs in by it",
    "DELETE /phones/{phone_id}": "a number the project uses; nobody logs in by it",
}
"""Routes that add or remove no way into the account, and why."""


def _calls(dependant: Dependant | None) -> set[Callable[..., Any]]:
    """Every dependency a route runs, its own included."""
    if dependant is None or dependant.call is None:
        return set()
    return {dependant.call}.union(*(_calls(each) for each in dependant.dependencies))


def account_routes(routes: Any) -> Iterator[tuple[str, bool]]:  # noqa: ANN401 - routes.
    """Each route a signed-in user reaches: its label, and whether it is checked."""
    for label, dependant in served_routes(routes):
        found = _calls(dependant)
        if public not in found:
            yield label, manages_logins in found


def test_every_route_that_changes_an_account_declares_its_check(app: App) -> None:
    """Checked, or listed with a reason: never neither, and no stale entry."""
    app()
    router = APIRouter()
    for name in ROUTERS:
        router.include_router(
            importlib.import_module(f"domainkit.contrib.{name}").router
        )

    changing = {
        label: checked
        for label, checked in account_routes(router.routes)
        if not label.startswith("GET ")
    }
    undeclared = sorted(
        label
        for label, checked in changing.items()
        if not checked and label not in DECIDES and label not in NO_WAY_IN
    )
    stale = sorted((DECIDES.keys() | NO_WAY_IN.keys()) - changing.keys())
    both = sorted(
        label
        for label, checked in changing.items()
        if checked and (label in DECIDES or label in NO_WAY_IN)
    )

    assert undeclared == [], (
        "Add dependencies=[MANAGES_LOGINS] to these routes, or list them in "
        "NO_WAY_IN or DECIDES with the reason."
    )
    assert stale == []
    assert both == []


def test_a_web_session_is_refused_by_every_checked_route_whatever_it_sends(
    app: App,
) -> None:
    """Phone modes: ``403 phone_required`` before the body is read; the phone passes."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)
    served: Any = web.app
    checked = [
        label for label, is_checked in account_routes(served.routes) if is_checked
    ]

    assert len(checked) >= 13  # Devices, passkeys, push tokens, factors, tokens.
    for label in checked:
        method, path = label.split(" ", 1)
        path = re.sub(r"\{[^}]+\}", str(uuid4()), path)
        stolen = web.request(method, path, json={}, headers=laptop)
        own = phone.send(method, path, {} if method != "GET" else None)
        assert refused(stolen), (label, stolen.status_code, stolen.json())
        assert not refused(own), (label, own.json())


def test_the_check_refuses_a_request_with_no_user() -> None:
    """Declared without a rule that asks for the user: 401, not let through."""
    import pytest
    from starlette.requests import Request

    from domainkit.http.access import NotAuthenticated
    from domainkit.management.base import run_async

    with pytest.raises(NotAuthenticated):
        run_async(manages_logins(Request({"type": "http"})))
