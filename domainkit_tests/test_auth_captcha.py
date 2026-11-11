"""A captcha on login, signup, and password reset (``AUTH.CAPTCHA``)."""

from typing import Any
from urllib.parse import parse_qsl

import httpx2
import pytest

from domainkit.contrib.auth.captcha import VERIFY, CaptchaUnavailable
from domainkit.http.client import stub
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import PASSWORD, create_user

TURNSTILE = {"PROVIDER": "turnstile", "SECRET": "site-secret"}


def verifier(verdict: dict[str, Any], seen: list[dict[str, str]]) -> Any:  # noqa: ANN401
    """A provider that answers ``verdict``, keeping each form it was sent."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) in VERIFY.values()
        seen.append(dict(parse_qsl(request.content.decode())))
        return httpx2.Response(200, json=verdict)

    return answer


def test_login_asks_for_a_solved_captcha_first(identity: Api) -> None:
    """No token or a refused one is 400 before the password is looked at."""
    client = identity(CAPTCHA=TURNSTILE)
    create_user("ann")
    credentials = {"username": "ann", "password": PASSWORD}
    seen: list[dict[str, str]] = []

    missing = client.post("/auth/login", json=credentials)
    with stub(verifier({"success": False}, seen)):
        refused = client.post(
            "/auth/login", json=credentials, headers={"X-Captcha-Token": "bad"}
        )
    with stub(verifier({"success": True}, seen)):
        solved = client.post(
            "/auth/login", json=credentials, headers={"X-Captcha-Token": "good"}
        )

    assert (missing.status_code, missing.json()["code"]) == (400, "captcha_failed")
    assert (refused.status_code, refused.json()["code"]) == (400, "captcha_failed")
    assert solved.status_code == 200
    assert seen[-1] == {"secret": "site-secret", "response": "good"}


def test_signup_and_password_reset_ask_too(identity: Api) -> None:
    """Every route ``ON`` names; others are left alone."""
    client = identity(CAPTCHA=TURNSTILE, SIGNUP_ENABLED=True)

    signup = client.post(
        "/auth/signup", json={"username": "bo", "email": "bo@example.com"}
    )
    reset = client.post("/auth/password-reset", json={"email": "bo@example.com"})
    login_only = identity(CAPTCHA={**TURNSTILE, "ON": ["login"]}, SIGNUP_ENABLED=True)
    open_signup = login_only.post(
        "/auth/signup", json={"username": "bo", "email": "bo@example.com"}
    )

    assert signup.status_code == reset.status_code == 400
    assert open_signup.status_code == 202


def test_a_low_recaptcha_score_or_an_unreachable_provider_is_refused(
    identity: Api,
) -> None:
    """Score under ``MIN_SCORE``: 400; no answer from the provider: 503."""
    recaptcha = {"PROVIDER": "recaptcha", "SECRET": "s", "MIN_SCORE": 0.7}
    client = identity(CAPTCHA=recaptcha)
    body = {"username": "x", "password": "y"}
    token = {"X-Captcha-Token": "t"}
    seen: list[dict[str, str]] = []

    with stub(verifier({"success": True, "score": 0.3}, seen)):
        low = client.post("/auth/login", json=body, headers=token)

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    with stub(down):
        unreachable = client.post("/auth/login", json=body, headers=token)
    with stub(lambda request: httpx2.Response(200, content=b"not json")):
        garbled = client.post("/auth/login", json=body, headers=token)
    with stub(verifier(["not", "an", "object"], seen)):  # type: ignore[arg-type]
        odd = client.post("/auth/login", json=body, headers=token)

    assert (low.status_code, low.json()["code"]) == (400, "captcha_failed")
    assert (unreachable.status_code, unreachable.json()["code"]) == (
        503,
        "captcha_unavailable",
    )
    assert garbled.status_code == 503
    assert odd.status_code == 400
    assert CaptchaUnavailable.status_code == 503


@pytest.mark.parametrize("token", ["", "x" * 4097])
def test_tokens_too_short_or_too_long_are_not_sent(identity: Api, token: str) -> None:
    """An empty token, or one past 4,096 characters, never reaches the provider."""
    client = identity(CAPTCHA=TURNSTILE)
    seen: list[dict[str, str]] = []

    with stub(verifier({"success": True}, seen)):
        answer = client.post(
            "/auth/login",
            json={"username": "x", "password": "y"},
            headers={"X-Captcha-Token": token},
        )

    assert answer.status_code == 400
    assert seen == []


def test_a_browser_login_asks_for_the_same_captcha(identity: Api) -> None:
    """``POST /auth/session`` checks a password too: it was left out of ``login``."""
    client = identity(CAPTCHA={**TURNSTILE, "ON": ["login"]})
    create_user("ann")
    credentials = {"username": "ann", "password": PASSWORD}
    origin = {"Origin": "http://testserver"}
    seen: list[dict[str, str]] = []

    missing = client.post("/auth/session", json=credentials, headers=origin)
    with stub(verifier({"success": True}, seen)):
        solved = client.post(
            "/auth/session",
            json=credentials,
            headers={**origin, "X-Captcha-Token": "good"},
        )

    assert (missing.status_code, missing.json()["code"]) == (400, "captcha_failed")
    assert solved.status_code == 200, solved.json()
    assert seen == [{"secret": "site-secret", "response": "good"}]
