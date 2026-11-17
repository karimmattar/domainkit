"""A held act keeps its authorization; each act asks its own methods (user report A17).

An admin change held for a second person's approval (approvals'
``approval_pending``) was made, once approved, by its maker's same call,
"with the first authorization while it lives": five minutes
(``AUTH.AUTHORIZATIONS.TTL``), so the maker confirmed it twice.
``AUTHORIZATIONS.HELD_TTL`` keeps it while the request waits, within that
long past its expiry (ADR 0174). The framework's acts took
``AUTHORIZATIONS.METHODS`` and its ``TTL``, the same for every act;
``AUTHORIZATIONS.ACTIONS`` gives one act, the framework's or the
project's, its own.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.auth.actions import (
    ActionAuthorization,
    Kept,
    Shown,
    _Accepted,
    _kept,
    action_for,
    hold_authorizations,
)
from domainkit.contrib.auth.apps import action_problems
from domainkit.contrib.auth.settings import AuthorizationsSettings
from domainkit_tests.support.auth import PASSWORD, create_user
from domainkit_tests.test_admin_actions import (
    ACTS,
    Staff,
    client,
    frozen,
    problem,
    row,
)
from domainkit_tests.test_approvals_guards import installed
from domainkit_tests.test_auth_stepup import now_code

__all__ = ["client", "frozen", "installed"]  # The fixtures the tests take.

AUTH: Final = ACTS.settings["AUTH"]
DAY: Final = timedelta(days=1)


def making_staff(
    client: TestClient, frozen: FrozenClock
) -> tuple[Staff, Staff, str, dict[str, bool]]:
    """Ann, who makes staff with approval; root, who approves; the path; the body."""
    ann = Staff(client, frozen, "ann")
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target")
    return ann, root, f"/users/{target}", {"is_staff": True}


def approve(root: Staff, request_id: str) -> None:
    """Root approves the request ``request_id``."""
    read = root.client.get(f"/approvals/requests/{request_id}", headers=root.headers)
    approved = root.client.post(
        f"/approvals/requests/{request_id}/approve",
        json={"fingerprint": read.json()["fingerprint"]},
        headers=root.headers,
    )
    assert approved.status_code == 200, approved.json()


def held_for(longest: timedelta) -> Any:  # noqa: ANN401 - a context manager.
    """The project's settings, with ``HELD_TTL`` ``longest``."""
    return override_settings(AUTH={**AUTH, "AUTHORIZATIONS": {"HELD_TTL": longest}})


def test_a_held_change_keeps_its_authorization_until_it_is_approved(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Approved two hours later, the same call with the same authorization makes it.

    Before 2.7 the authorization ended five minutes after it was asked
    for, and the maker confirmed the change again once approved.
    """
    ann, root, path, making = making_staff(client, frozen)
    with held_for(DAY):
        authorization = ann.authorized(f"{path}/authorization/patch", making)
        waiting = ann.call("PATCH", path, making, authorization)
        held = row(authorization)
        frozen.tick(timedelta(hours=2))
        approve(root, waiting.json()["approval_request_id"])
        made = ann.call("PATCH", path, making, authorization)

    problem(waiting, 403, "approval_pending")
    assert held["held_until"] == held["expires_at"] + DAY  # Before the request's.
    assert made.status_code == 200, made.json()
    assert made.json()["is_staff"] is True
    assert row(authorization)["consumed_at"] is not None


def test_without_held_ttl_a_held_change_needs_a_new_authorization(
    client: TestClient, frozen: FrozenClock
) -> None:
    """``HELD_TTL`` None, the default: as before 2.7, it expires with its ``TTL``."""
    ann, root, path, making = making_staff(client, frozen)
    authorization = ann.authorized(f"{path}/authorization/patch", making)
    waiting = ann.call("PATCH", path, making, authorization)
    frozen.tick(timedelta(hours=2))
    approve(root, waiting.json()["approval_request_id"])
    late = ann.call("PATCH", path, making, authorization)
    again = ann.authorized(f"{path}/authorization/patch", making)
    made = ann.call("PATCH", path, making, again)

    assert row(authorization)["held_until"] is None
    problem(late, 403, "action_not_authorized")
    assert made.status_code == 200, made.json()


def test_a_hold_ends_with_the_request_and_within_held_ttl_of_its_expiry(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Kept until the request expires (three days), and ``HELD_TTL`` bounds it.

    ``HELD_TTL`` lowered once it is held bounds it too: the code covers
    its expiry, and the hold counts within that setting of it.
    """
    ann, root, path, making = making_staff(client, frozen)
    with held_for(7 * DAY):
        authorization = ann.authorized(f"{path}/authorization/patch", making)
        waiting = ann.call("PATCH", path, making, authorization)
    held = row(authorization)
    frozen.tick(2 * DAY)
    approve(root, waiting.json()["approval_request_id"])
    with held_for(DAY):
        lowered = ann.call("PATCH", path, making, authorization)
    with held_for(7 * DAY):
        made = ann.call("PATCH", path, making, authorization)

    problem(waiting, 403, "approval_pending")
    assert (
        held["expires_at"] + 2 * DAY < held["held_until"] < held["expires_at"] + 3 * DAY
    )
    problem(lowered, 403, "action_not_authorized")
    assert made.status_code == 200, made.json()


def test_only_what_an_act_took_and_left_unspent_is_held() -> None:
    """Outside a request nothing is; within one, an act's untaken or spent stay.

    Two holds keep the later time.
    """
    soon = datetime(2027, 1, 5, tzinfo=UTC)
    later = soon + DAY
    stored = ActionAuthorization(
        id=uuid4(),
        user_id=uuid4(),
        login="login",
        purpose="auth.admin_change",
        subject="auth.user:new",
        digest="0" * 64,
        shown=Shown("Create a user"),
        created_at=soon,
        expires_at=soon,
    )
    hold_authorizations(soon)  # No request: nothing to hold.
    kept = Kept()
    untaken = _Accepted(stored, "code", "POST /")
    spent = _Accepted(stored, "code", "POST /", taken=True, consumed=True)
    taken = _Accepted(stored, "code", "POST /", taken=True)
    kept.accepted = {"a": untaken, "b": spent, "c": taken}
    token = _kept.set(kept)
    try:
        hold_authorizations(later)
        hold_authorizations(soon)
    finally:
        _kept.reset(token)

    assert (untaken.held, spent.held, taken.held) == (None, None, later)


# Each act its own methods and lifetime ---------------------------------------


def test_the_admin_change_asks_its_own_methods_and_lives_its_own_ttl(
    client: TestClient, frozen: FrozenClock
) -> None:
    """``ACTIONS["auth.admin_change"]``: the password too, and twenty minutes.

    The project's acts keep ``METHODS``, a code; the admin API's changes
    ask what the setting names, and a code alone no longer confirms them.
    """
    root = Staff(client, frozen, "root", superuser=True)
    lives = timedelta(minutes=20)
    own = {"METHODS": ["pwd", "otp"], "TTL": lives}
    with override_settings(
        AUTH={**AUTH, "AUTHORIZATIONS": {"ACTIONS": {"auth.admin_change": own}}}
    ):
        twenty_minutes = (frozen.now() + lives).isoformat().replace("+00:00", "Z")
        asked = root.ask("/users/authorization", {"username": "made"})
        authorization_id = asked.json()["id"]
        url = f"/auth/authorizations/{authorization_id}/confirm"
        code = now_code(root.secret, frozen)
        bare = client.post(url, json={"code": code}, headers=root.headers)
        confirmed = client.post(
            url, json={"code": code, "password": PASSWORD}, headers=root.headers
        )
        submit = action_for("approvals.submit")

    assert asked.status_code == 201, asked.json()
    assert asked.json()["methods"] == ["otp", "pwd"]
    assert asked.json()["expires_at"] == twenty_minutes
    problem(bare, 400, "password_required")
    assert confirmed.status_code == 200, confirmed.json()
    assert submit is not None
    assert submit.required() == {"otp"}  # Not named: AUTHORIZATIONS.METHODS.
    assert submit.lifetime() == timedelta(minutes=5)


def test_check_refuses_an_act_the_setting_names_that_no_action_has(
    client: TestClient,
) -> None:
    """``auth.E112``: a purpose nothing registered; ``E108``: methods none proves."""
    named = {"auth.admin_change": {"METHODS": ["phone"]}, "auth.admin_chnage": {}}
    with override_settings(AUTH={**AUTH, "AUTHORIZATIONS": {"ACTIONS": named}}):
        problems = action_problems(FastAPI())

    assert [(each.id, "admin_chnage" in each) for each in problems] == [
        ("auth.E112", True),
        ("auth.E108", False),
    ]


@pytest.mark.parametrize(
    "authorizations",
    [
        {"HELD_TTL": timedelta(days=8)},
        {"HELD_TTL": timedelta(0)},
        {"ACTIONS": {"Admin Change": {}}},
        {"ACTIONS": {"auth.admin_change": {"METHODS": ["pwd"]}}},
        {"ACTIONS": {"auth.admin_change": {"METHODS": ["otp", "otp"]}}},
        {"ACTIONS": {"auth.admin_change": {"TTL": timedelta(minutes=31)}}},
        {"ACTIONS": {"auth.admin_change": {"WINDOW": 5}}},
    ],
)
def test_the_settings_are_bounded(authorizations: dict[str, Any]) -> None:
    """A week of hold at most; a purpose's name; a second factor; 30 minutes."""
    with pytest.raises(ValidationError):
        AuthorizationsSettings(**authorizations)


def test_the_settings_hash_by_every_key() -> None:
    """Cached by value: an act's own entry counts."""
    plain = AuthorizationsSettings()
    phone = AuthorizationsSettings(
        ACTIONS={"auth.admin_change": {"METHODS": ["phone"]}}
    )

    assert hash(plain) == hash(AuthorizationsSettings())
    assert hash(plain) != hash(phone)


def test_without_auth_a_held_call_keeps_no_authorization(installed: None) -> None:
    """Approvals keeps the held call's authorization only where auth is installed."""
    from domainkit.contrib.approvals.services import _keep_authorizations

    _keep_authorizations(datetime(2027, 1, 5, tzinfo=UTC))  # Nothing to keep.
