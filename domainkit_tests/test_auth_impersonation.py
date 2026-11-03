"""Support staff acting as a user, audited and bounded (``AUTH.IMPERSONATION_TTL``)."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.authentication import Credentials
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_access import request, user
from domainkit_tests.test_auth_admin import details, error, staff, token_for
from domainkit_tests.test_auth_http import audited, bearer, create_user

ON = {"IMPERSONATION_TTL": timedelta(minutes=30)}


def impersonate(
    client: TestClient,
    target: object,
    headers: dict[str, str],
    reason: str = "Ticket 42",
) -> Any:  # noqa: ANN401 - a response.
    """Ask to act as the user ``target``."""
    return client.post(
        f"/admin/auth/users/{target}/impersonate",
        json={"reason": reason},
        headers=headers,
    )


def test_a_superuser_acts_as_a_user_and_the_trail_names_them(api: Api) -> None:
    """The token is the user's; what it does is recorded as the staff member's."""
    client = api(**ON)
    boss = staff("boss", is_superuser=True)
    ann = create_user("ann")

    started = impersonate(client, ann, boss)

    assert started.status_code == 201, started.json()
    body = started.json()
    assert (body["token_type"], body["expires_in"]) == ("Bearer", 1800)
    as_ann = bearer(body["access_token"])
    me = client.get("/auth/me", headers=as_ann)
    assert me.json()["username"] == "ann"
    (event,) = audited("auth.user_impersonated")
    boss_id = client.get("/auth/me", headers=boss).json()["id"]
    assert event["actor"] == boss_id
    assert details("auth.user_impersonated")[0]["impersonator_id"] == boss_id
    client.post("/auth/logout", headers=as_ann)
    (logged_out,) = audited("auth.user_logged_out")
    assert logged_out["actor"] == boss_id  # Acts done as ann are the boss's.
    assert client.get("/auth/me", headers=as_ann).status_code == 401


def test_impersonation_is_off_by_default_and_never_of_oneself(api: Api) -> None:
    """403 while the setting is unset, for oneself, and for inactive users."""
    off = api()
    boss = staff("boss", is_superuser=True)
    ann = create_user("ann")
    gone = create_user("gone", is_active=False)

    assert error(impersonate(off, ann, boss), 403)[1].startswith("Impersonation is off")
    client = api(**ON)
    boss_id = client.get("/auth/me", headers=boss).json()["id"]
    assert "themselves" in error(impersonate(client, boss_id, boss), 403)[1]
    assert "active users" in error(impersonate(client, gone, boss), 403)[1]
    assert error(impersonate(client, uuid4(), boss), 404)[0] == "user_not_found"
    assert audited("auth.user_impersonated") == []


def test_staff_impersonate_only_users_who_hold_no_more_than_they_do(api: Api) -> None:
    """A clerk with ``change_user`` may not act as a superuser, nor without a reason."""
    client = api(**ON)
    clerk = staff("clerk", "auth.change_user")
    root = create_user("root", is_superuser=True)
    ann = create_user("ann")

    assert error(impersonate(client, root, clerk), 403)[0] == "permission_denied"
    assert impersonate(client, ann, clerk).status_code == 201
    assert impersonate(client, ann, clerk, reason="").status_code == 422
    plain = token_for("ann")
    assert impersonate(client, root, plain).status_code == 403


@pytest.mark.anyio
async def test_impersonated_credentials_never_step_up() -> None:
    """The acts a step-up guards are the user's own."""
    who = user()
    impersonated = request(user=who)
    impersonated.scope["auth"] = Credentials(who, scheme="token", impersonator=uuid4())

    assert not await StepUp(max_age=timedelta(minutes=5)).has_permission(
        impersonated, who
    )
