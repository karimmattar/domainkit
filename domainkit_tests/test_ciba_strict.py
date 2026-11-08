"""``CIBA.APPROVE_LOGINS = "always"``: every login comes from a bound phone."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from domainkit.conf import override_settings
from domainkit.contrib.ciba.settings import CibaSettings
from domainkit_tests.test_auth_cookies import TRUSTED
from domainkit_tests.test_auth_http import PASSWORD, create_user
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
    create_user("alice", email="alice@example.com")
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
    assert (refused.status_code, refused.json()["code"]) == (
        403,
        "phone_enrollment_required",
    )
    assert session.json()["code"] == "phone_enrollment_required"
    assert from_the_app.json()["code"] == "phone_enrollment_required"
    assert enrolled.status_code == 201, enrolled.json()
    assert bound.json()["code"] == "phone_approval_required"


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
