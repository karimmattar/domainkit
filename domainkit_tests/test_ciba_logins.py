"""The phone sees the user's web logins and ends them (ADR 0130).

With ``CIBA.APPROVE_LOGINS`` set, only a request the user's bound device
signs may list the logins or end one that is not its own.
"""

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from domainkit import clock
from domainkit_tests.support.auth import PASSWORD, bearer, create_user
from domainkit_tests.test_ciba import (
    LAPTOP,
    PHONE,
    App,
    Phone,
    app,
    redeem,
    start_push,
)

__all__ = ["app"]  # The fixture the tests take.

PHONE_REQUIRED = "phone_required"


def web_login(web: TestClient, phone: Phone) -> dict[str, str]:
    """Sign the web page in by a push the phone approves: its bearer header."""
    started = start_push(web)
    [waiting] = phone.waiting()
    approved = phone.approve(waiting["id"], match_code=started["match_code"])
    assert approved.status_code == 204, approved.json()
    with clock.freeze_time(clock.now() + timedelta(seconds=6)):
        tokens = redeem(web, started["request"])
    assert tokens.status_code == 200, tokens.json()
    return bearer(tokens.json()["access_token"])


def refused(answer: Any) -> bool:  # noqa: ANN401 - a response.
    """Whether ``answer`` is the phone-only refusal."""
    return bool(answer.status_code == 403 and answer.json()["code"] == PHONE_REQUIRED)


def test_the_phone_lists_the_web_logins_and_ends_one_or_the_others(
    app: App,
) -> None:
    """Signed by the device: listed with where each came from, then ended."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)

    answer = phone.send("GET", "/auth/tokens")

    assert answer.status_code == 200, answer.json()
    by_method = {item["method"]: item for item in answer.json()["items"]}
    assert set(by_method) == {"password", "push"}
    assert by_method["password"]["current"] is True
    assert by_method["password"]["ip"] == PHONE[0]
    assert (by_method["push"]["ip"], by_method["push"]["current"]) == (
        LAPTOP[0],
        False,
    )
    ended = phone.send("DELETE", f"/auth/tokens/{by_method['push']['id']}")
    assert ended.status_code == 204
    assert web.get("/auth/me", headers=laptop).status_code == 401

    first, second = web_login(web, phone), web_login(web, phone)
    others = phone.send("POST", "/auth/logout", {"others": True})
    assert others.status_code == 204
    assert web.get("/auth/me", headers=first).status_code == 401
    assert web.get("/auth/me", headers=second).status_code == 401
    assert phone.send("GET", "/auth/me").status_code == 200


def test_a_web_login_cannot_list_or_end_the_others(app: App) -> None:
    """Not signed by the device: ``403 phone_required``; its own logout stays."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    laptop = web_login(web, phone)
    [own] = [
        item["id"]
        for item in phone.send("GET", "/auth/tokens").json()["items"]
        if item["current"]
    ]

    answers = [
        web.get("/auth/tokens", headers=laptop),
        web.delete(f"/auth/tokens/{own}", headers=laptop),
        web.post("/auth/logout", headers=laptop, json={"others": True}),
        web.post("/auth/logout", headers=laptop, json={"everywhere": True}),
        phone.client.get("/auth/tokens", headers=phone.headers),  # Unsigned.
    ]

    assert all(refused(answer) for answer in answers), [a.json() for a in answers]
    assert phone.send("GET", "/auth/me").status_code == 200
    assert web.post("/auth/logout", headers=laptop).status_code == 204
    assert web.get("/auth/me", headers=laptop).status_code == 401


def test_strict_mode_asks_for_the_phone_too(app: App) -> None:
    """``APPROVE_LOGINS = "always"`` is a phone mode as well."""
    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app(APPROVE_LOGINS="always")
    laptop = web_login(web, phone)

    assert refused(web.get("/auth/tokens", headers=laptop))
    assert phone.send("GET", "/auth/tokens").status_code == 200


def test_users_without_a_phone_or_outside_the_phone_modes_manage_as_before(
    app: App,
) -> None:
    """No bound device, or ``APPROVE_LOGINS`` off: no signature is asked for."""
    web = app(APPROVE_LOGINS=True)
    create_user("bob")
    signed_in = web.post(
        "/auth/login", json={"identifier": "bob", "password": PASSWORD}
    )
    bob = bearer(signed_in.json()["access_token"])
    assert web.get("/auth/tokens", headers=bob).status_code == 200

    phone = Phone(app(PHONE, APPROVE_LOGINS=False))
    unsigned = phone.client.get("/auth/tokens", headers=phone.headers)
    assert unsigned.status_code == 200
    assert {item["method"] for item in unsigned.json()["items"]} == {"password"}
