"""``CIBA.APPROVE_LOGINS = "always"``: every login comes from a bound phone."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from domainkit.conf import override_settings
from domainkit.contrib.ciba.settings import CibaSettings
from domainkit_tests.support.auth import PASSWORD, create_user
from domainkit_tests.test_auth_cookies import TRUSTED
from domainkit_tests.test_ciba import (
    PHONE,
    App,
    Phone,
    app,
    redeem,
    start_push,
)
from domainkit_tests.test_devices import Device
from domainkit_tests.test_devices_enrollment import (
    AUTH,
    ON,
    _identifiers,
    ask_code,
    enroll,
    mailed_code,
)

__all__ = ["_identifiers", "app"]  # The fixtures the tests take.

ALWAYS = "always"


def log_in(client: TestClient, identifier: str = "alice") -> Any:  # noqa: ANN401 - a response.
    """``POST /auth/login`` with the password: the response."""
    return client.post(
        "/auth/login", json={"identifier": identifier, "password": PASSWORD}
    )


def test_the_whole_cycle_runs_from_the_phone(app: App) -> None:
    """No password login anywhere; enroll, then the phone signs or approves."""
    web = app(APPROVE_LOGINS=ALWAYS)
    phone_client = app(PHONE, APPROVE_LOGINS=ALWAYS)
    create_user("alice", email="alice@example.com", proved=True)
    with override_settings(DEVICES=ON):
        refused = log_in(web)
        session = web.post(
            "/auth/session",
            json={"identifier": "alice", "password": PASSWORD},
            headers=TRUSTED,
        )
        from_the_app = log_in(phone_client)  # Not signed: no device yet.

        ask_code(phone_client, "alice@example.com")
        device = Device()
        enrolled = enroll(phone_client, device, code=mailed_code())

        bound = log_in(web)
    # One answer before the password, device or not: it says nothing of
    # the account (since 2.4; it was phone_enrollment_required).
    assert (refused.status_code, refused.json()["code"]) == (
        403,
        "phone_approval_required",
    )
    assert session.json()["code"] == "phone_approval_required"
    assert from_the_app.json()["code"] == "phone_approval_required"
    assert enrolled.status_code == 201, enrolled.json()
    assert bound.json()["code"] == "phone_approval_required"


def test_a_password_login_no_device_signed_learns_nothing(app: App) -> None:
    """``"always"``: refused before the password, the same for everyone.

    A wrong password, an unknown account, and a right one get one answer,
    no wrong password is counted, and none is audited as such: the route
    could never log them in.
    """
    from domainkit_tests.support.auth import audited

    web = app(APPROVE_LOGINS=ALWAYS)
    create_user("alice", email="alice@example.com", proved=True)
    auth = {**AUTH, "LOGIN_RATE": "2/minute"}
    with override_settings(DEVICES=ON, AUTH=auth):
        answers = [
            web.post("/auth/login", json={"identifier": who, "password": password})
            for who, password in (
                ("alice", "wrong-password-1"),
                ("nobody", PASSWORD),
                ("alice", PASSWORD),
                ("alice", "wrong-password-2"),
            )
        ]

    assert {(a.status_code, a.json()["code"]) for a in answers} == {
        (403, "phone_approval_required")
    }
    assert audited("auth.login") == []  # No wrong password was even checked.


def test_bound_users_sign_in_as_with_true(app: App) -> None:
    """A bound user: the phone's signed login, and approvals, still work."""
    phone = Phone(app(PHONE))  # Bound before the setting, as by enrolling.
    web = app(APPROVE_LOGINS=ALWAYS)
    signed = phone.send(
        "POST", "/auth/login", {"identifier": "alice", "password": PASSWORD}
    )
    started = start_push(web)
    phone.approve(phone.waiting()[0]["id"], match_code=started["match_code"])
    assert signed.status_code == 200, signed.json()
    assert redeem(web, started["request"]).status_code == 200


def test_the_setting_takes_true_false_or_always() -> None:
    """Anything else is refused at startup."""
    assert CibaSettings(APPROVE_LOGINS=ALWAYS).APPROVE_LOGINS == ALWAYS
    assert CibaSettings(APPROVE_LOGINS=True).APPROVE_LOGINS is True
    with pytest.raises(ValidationError):
        CibaSettings(APPROVE_LOGINS="sometimes")


def test_always_without_enrollment_is_a_problem(app: App) -> None:
    """E005: nobody could log in; enrollment on, the problem goes."""
    from fastapi import FastAPI

    from domainkit.apps import registry
    from domainkit.contrib.ciba.api import router as sign_in_router
    from domainkit.contrib.devices.api import router as devices_router

    app()
    config = registry.get_domain_config("ciba")
    served = FastAPI()
    served.include_router(sign_in_router)
    served.include_router(devices_router)

    def problems() -> list[str]:
        found = config.application_problems(served)  # type: ignore[attr-defined]
        return [problem.id for problem in found]

    with override_settings(CIBA={"APPROVE_LOGINS": ALWAYS}):
        assert problems() == ["ciba.E005"]
        with override_settings(DEVICES=ON):
            assert problems() == []
    with override_settings(CIBA={"APPROVE_LOGINS": True}):
        assert problems() == []


def test_a_refused_login_replaces_no_password_staff_set(app: App) -> None:
    """The gates run before a password staff set is changed on first use.

    A web login the phone did not approve, sending a new password, is
    refused: the password stays the one staff set, and the phone's own
    login still works (a change would have ended it).
    """
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.contrib.auth.models import UserId
    from domainkit.management.base import run_async

    phone = Phone(app(PHONE, APPROVE_LOGINS=True))
    web = app()
    staff_set, chosen = "Kq7-zv93-Lmw2", "Pw4-tt81-Xcv9"

    async def set_by_staff() -> None:
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get(UserId(phone.user_id))
            await uow.users.set_password(stored, staff_set, temporary=True)
            await uow.commit()

    first_use = {**AUTH, "PASSWORD_CHANGE_ON_FIRST_USE": True}
    with override_settings(AUTH=first_use):
        run_async(set_by_staff())
        refused = web.post(
            "/auth/login",
            json={"identifier": "alice", "password": staff_set, "new_password": chosen},
        )
        with_chosen = web.post(
            "/auth/login", json={"identifier": "alice", "password": chosen}
        )

    assert (refused.status_code, refused.json()["code"]) == (
        403,
        "phone_approval_required",
    )
    assert with_chosen.json()["code"] == "invalid_credentials"  # Not replaced.
    assert phone.client.get("/auth/me", headers=phone.headers).status_code == 200
