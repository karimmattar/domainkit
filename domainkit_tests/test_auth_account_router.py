"""``account_router`` mounts the account routes of every installed app."""

import pytest
from fastapi import APIRouter

from domainkit.conf import override_settings
from domainkit.contrib.auth.routing import (
    AccountRoutes,
    account_router,
    account_routes,
    on_account_routes,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import served_routes
from domainkit_tests.test_ciba import App, app

__all__ = ["app"]  # The fixture the tests take.

COOKIES = "domainkit.contrib.auth.cookies.CookieAuthentication"


def paths(router: APIRouter) -> set[str]:
    """The paths ``router`` serves."""
    return {label.split(" ", 1)[1] for label, _ in served_routes(router.routes)}


def test_it_mounts_what_the_installed_apps_name(app: App) -> None:
    """Auth's, the devices', and the sign-in approvals', in one router."""
    app()

    assert list(account_routes()) == [
        "login",
        "sessions",
        "mfa",
        "tokens",
        "step-up",
        "sca",
        "recovery",
        "signup",
        "devices",
        "sign-in",
    ]
    served = paths(account_router())
    assert {
        "/login",
        "/mfa/totp",
        "/tokens",
        "/step-up",
        "/sca/{challenge_id}/confirm",
        "/password-reset",
        "/devices",
        "/app-sign-in/push",
    } <= served
    assert "/signup" not in served  # Anyone could sign up: a project chooses.


def test_a_project_includes_and_excludes_by_name(app: App) -> None:
    """``include`` adds what is off by default; ``exclude`` leaves out."""
    app()

    chosen = paths(account_router(include=["signup"], exclude=["tokens", "devices"]))

    assert "/signup" in chosen
    assert "/tokens" not in chosen
    assert "/devices" not in chosen
    assert "/login" in chosen


def test_sessions_and_recovery_follow_what_serves_them(app: App) -> None:
    """Mounted only where they work: the cookie class, the apps that mail."""
    app()
    classes = ["domainkit.contrib.auth.authentication.TokenAuthentication"]

    with override_settings(AUTH={"AUTHENTICATION_CLASSES": [*classes, COOKIES]}):
        with_cookies = paths(account_router())
    with override_settings(AUTH={"AUTHENTICATION_CLASSES": classes}):
        without = paths(account_router())
        asked = paths(account_router(include=["sessions"]))

    assert "/session" in with_cookies
    assert "/session" not in without
    assert "/session" in asked  # A project may still ask for them.
    assert account_routes()["recovery"].default is True  # Messaging and mail are in.


def test_a_name_no_app_has_is_refused(app: App) -> None:
    """A typo, or routes of an app that is not installed: named, with what exists."""
    app()

    with pytest.raises(ImproperlyConfigured, match=r"identity.*there are login"):
        account_router(include=["identity"])
    with pytest.raises(ImproperlyConfigured, match="phones"):
        account_router(exclude=["phones"])


def test_two_apps_cannot_take_one_name(app: App) -> None:
    """The second ``login`` would shadow auth's."""
    app()
    on_account_routes(
        lambda: [AccountRoutes("login", "shop.api.router")], domain="ciba"
    )

    with pytest.raises(ImproperlyConfigured, match="Two apps name account routes"):
        account_routes()
