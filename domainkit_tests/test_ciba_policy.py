"""What a page's sign-ins ask of the phone, page by page (ADR 0155, user report A8).

``NUMBER_MATCHING`` and ``QR_SAME_NETWORK`` were single settings: a project
could not ask the number for its dashboard and not for its shop, and a QR
sign-in never asked one. An ``on_start`` hook now answers a
``SignInPolicy``; the settings decide what no hook gives.
"""

from typing import Any

import pytest
from starlette.requests import Request

from domainkit.contrib.ciba.hooks import SignInPolicy, on_approve, on_start
from domainkit.ddd import PermissionDenied
from domainkit_tests.test_ciba import (
    LAPTOP,
    PHONE,
    App,
    Phone,
    app,
    redeem,
    rows,
    start_push,
)

__all__ = ["app"]  # The fixture the tests take.

DASHBOARD = "Sign in to the dashboard"
CLAIM = "/auth/app-sign-in/requests/claim"


async def by_page(request: Request) -> SignInPolicy | None:
    """The dashboard asks the number and the same network; the shop neither."""
    page = request.headers.get("x-page")
    if page == "dashboard":
        return SignInPolicy(
            binding_message=DASHBOARD, number_matching=True, same_network="require"
        )
    if page == "shop":
        return SignInPolicy(number_matching=False, same_network="warn")
    return None


def push(web: Any, page: str | None = None) -> dict[str, Any]:  # noqa: ANN401
    """Start a push sign-in of alice from ``page``: what it answers."""
    answer = web.post(
        "/auth/app-sign-in/push",
        json={"identifier": "alice"},
        headers={"X-Page": page} if page else {},
    )
    assert answer.status_code == 200, answer.json()
    started: dict[str, Any] = answer.json()
    return started


def qr(web: Any, page: str | None = None) -> dict[str, Any]:  # noqa: ANN401
    """Start a QR sign-in from ``page``: what it answers."""
    answer = web.post("/auth/app-sign-in/qr", headers={"X-Page": page} if page else {})
    assert answer.status_code == 200, answer.json()
    started: dict[str, Any] = answer.json()
    return started


@pytest.mark.parametrize("setting", [True, False])
def test_a_page_asks_the_number_its_policy_says(app: App, setting: bool) -> None:
    """The dashboard always, the shop never; a page no hook names, the setting."""
    web = app(NUMBER_MATCHING=setting)
    phone = Phone(app(PHONE))
    on_start(by_page, domain="ciba")

    dashboard = push(web, "dashboard")
    shop = push(web, "shop")
    plain = push(web)

    assert len(dashboard["match_code"]) == 2
    assert "match_code" not in shop
    assert ("match_code" in plain) is setting
    shown = phone.waiting()  # Oldest first.
    assert [each["binding_message"] for each in shown] == [DASHBOARD, None, None]
    assert [each["match_required"] for each in shown] == [True, False, setting]
    approved = phone.approve(shown[0]["id"], match_code=dashboard["match_code"])
    assert approved.status_code == 204
    assert phone.approve(shown[1]["id"]).status_code == 204  # No number asked.
    assert redeem(web, dashboard["request"]).status_code == 200
    assert redeem(web, shop["request"]).status_code == 200


def test_a_qr_code_asks_the_number_under_the_setting(app: App) -> None:
    """``QR_NUMBER_MATCHING``: the page shows two digits; one wrong try denies it."""
    web = app(QR_NUMBER_MATCHING=True)
    phone = Phone(app(PHONE))

    first = qr(web)
    assert set(first) == {
        "request",
        "expires_in",
        "interval",
        "match_code",
        "qr",
        "qr_expires_in",
    }
    assert len(first["match_code"]) == 2
    assert first["match_code"].isdigit()
    claimed = phone.send("POST", CLAIM, {"code": first["qr"]}).json()
    assert claimed["match_required"] is True
    wrong = f"{(int(first['match_code']) + 1) % 100:02d}"
    refused = phone.approve(claimed["id"], match_code=wrong)
    assert refused.status_code == 422
    assert refused.json()["code"] == "wrong_match_code"
    assert redeem(web, first["request"]).json()["code"] == "access_denied"

    second = qr(web)
    shown = phone.send("POST", CLAIM, {"code": second["qr"]}).json()
    assert phone.approve(shown["id"], match_code=second["match_code"]).status_code == (
        204
    )
    session = redeem(web, second["request"], "session")
    assert session.status_code == 200, session.json()


def test_a_page_asks_the_number_of_its_qr_codes_or_not(app: App) -> None:
    """Off by default; the dashboard asks it, and the shop turns the setting off."""
    web = app()
    Phone(app(PHONE))
    on_start(by_page, domain="ciba")

    assert "match_code" not in qr(web)  # As before: no page asks it.
    assert len(qr(web, "dashboard")["match_code"]) == 2
    web = app(QR_NUMBER_MATCHING=True)
    assert "match_code" not in qr(web, "shop")
    assert [row["match_code"] is not None for row in rows()] == [False, True, False]


def test_a_page_can_require_the_same_network_for_its_qr_codes(app: App) -> None:
    """The setting warns; the dashboard's codes refuse a phone elsewhere."""
    web = app()
    far = Phone(app(PHONE))
    near = Phone(app(LAPTOP), "bob")
    on_start(by_page, domain="ciba")

    dashboard = qr(web, "dashboard")
    refused = far.send("POST", CLAIM, {"code": dashboard["qr"]})
    assert refused.status_code == 403
    assert refused.json()["code"] == "different_network"
    claimed = near.send("POST", CLAIM, {"code": dashboard["qr"]})
    assert claimed.status_code == 200, claimed.json()
    assert claimed.json()["same_network"] is True

    plain = far.send("POST", CLAIM, {"code": qr(web)["qr"]})
    assert plain.status_code == 200, plain.json()
    assert plain.json()["same_network"] is False  # Warned, as the setting says.
    assert [row["same_network"] for row in rows()] == ["require", None]


def test_a_page_may_warn_where_the_setting_requires(app: App) -> None:
    """``QR_SAME_NETWORK = "require"``; the shop's codes warn, the rest refuse."""
    web = app(QR_SAME_NETWORK="require")
    far = Phone(app(PHONE))
    on_start(by_page, domain="ciba")

    shop = far.send("POST", CLAIM, {"code": qr(web, "shop")["qr"]})
    assert shop.status_code == 200, shop.json()
    assert shop.json()["same_network"] is False
    plain = far.send("POST", CLAIM, {"code": qr(web)["qr"]})
    assert plain.status_code == 403  # No page's policy: the setting's.
    assert plain.json()["code"] == "different_network"


def test_the_strictest_policy_given_wins(app: App) -> None:
    """Of two hooks, the number and the same network are asked if one asks them."""
    web = app(NUMBER_MATCHING=False)
    far = Phone(app(PHONE))

    async def relaxed(request: Request) -> SignInPolicy:
        return SignInPolicy(
            binding_message="First", number_matching=False, same_network="warn"
        )

    async def strict(request: Request) -> SignInPolicy:
        return SignInPolicy(
            binding_message="Second", number_matching=True, same_network="require"
        )

    async def silent(request: Request) -> SignInPolicy:
        return SignInPolicy()

    on_start(relaxed, domain="ciba")
    on_start(silent, domain="ciba")
    on_start(strict, domain="ciba")

    assert len(start_push(web)["match_code"]) == 2
    refused = far.send("POST", CLAIM, {"code": qr(web)["qr"]})
    assert refused.json()["code"] == "different_network"
    assert [row["binding_message"] for row in rows()] == ["First", "First"]


@pytest.mark.parametrize(
    "answer",
    [
        SignInPolicy(same_network="always"),  # type: ignore[arg-type]
        SignInPolicy(number_matching="yes"),  # type: ignore[arg-type]
        SignInPolicy(binding_message=7),  # type: ignore[arg-type]
        SignInPolicy(binding_message="x" * 65),
        42,
    ],
    ids=["network", "matching", "message type", "message length", "other"],
)
def test_a_policy_the_start_cannot_apply_is_a_mistake(
    app: App,
    answer: object,
) -> None:
    """The project's mistake, reported; nothing is stored."""
    web = app()

    async def wrong(request: Request) -> Any:  # noqa: ANN401
        return answer

    on_start(wrong, domain="ciba")
    assert web.post("/auth/app-sign-in/qr").status_code == 500
    assert rows() == []


def test_staff_approve_only_the_sign_ins_that_asked_the_number(app: App) -> None:
    """Anyone starts from any page: ``on_approve`` keeps the number for staff."""
    web = app()
    phone = Phone(app(PHONE), "alice", is_staff=True)
    on_start(by_page, domain="ciba")

    async def staff_need_the_number(
        request: Request,
        user: Any,  # noqa: ANN401
        sign_in: Any,  # noqa: ANN401
    ) -> None:
        if user.is_staff and sign_in.kind == "sign_in" and sign_in.match_code is None:
            raise PermissionDenied("Sign in from a page that shows a number.")

    on_approve(staff_need_the_number, domain="ciba")
    shop = push(web, "shop")
    [waiting] = phone.waiting()
    refused = phone.approve(waiting["id"])
    assert refused.status_code == 403
    assert redeem(web, shop["request"]).json()["code"] == "access_denied"

    dashboard = push(web, "dashboard")
    asked = next(each for each in phone.waiting() if each["match_required"])
    approved = phone.approve(asked["id"], match_code=dashboard["match_code"])
    assert approved.status_code == 204
    assert redeem(web, dashboard["request"]).status_code == 200
