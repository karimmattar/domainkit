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

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.approvals import ApprovalsUnitOfWork
from domainkit.contrib.approvals.services import (
    CancelRequest,
    RejectRequest,
    held_request_waits,
    record_cancellation,
    record_rejection,
)
from domainkit.contrib.auth.actions import (
    ActionAuthorization,
    ActionNotAuthorized,
    Kept,
    Shown,
    _Accepted,
    _kept,
    action_for,
    hold_authorizations,
    refuse_held_elsewhere,
)
from domainkit.contrib.auth.apps import action_problems
from domainkit.contrib.auth.settings import AuthorizationsSettings
from domainkit.management.base import run_async
from domainkit_tests.support.auth import PASSWORD, create_user
from domainkit_tests.test_admin_actions import (
    ACTS,
    Staff,
    client,
    frozen,
    held_requests,
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


def end_request(request_id: UUID, ending: str, maker: str, approver: UUID) -> None:
    """Reject the request ``request_id``, or let its maker withdraw it."""
    if ending == "reject":
        rejected = RejectRequest(
            request_id=request_id, approver=str(approver), reason="Not now"
        )
        run_async(record_rejection(rejected, ApprovalsUnitOfWork()))
    else:
        cancelled = CancelRequest(request_id=request_id, maker=maker)
        run_async(record_cancellation(cancelled, ApprovalsUnitOfWork()))


@pytest.mark.parametrize("ending", ["reject", "cancel"])
def test_a_hold_ends_with_its_request_rejected_or_withdrawn(
    client: TestClient, frozen: FrozenClock, ending: str
) -> None:
    """The request ended: the held authorization works no more, for it or a new one.

    Held until the request's expiry alone, the same header submitted a new
    request hours after a rejection or the maker's withdrawal, with no new
    confirmation, and made the change once that was approved (found in
    review). Within its own five minutes too, it serves no other request.
    """
    ann, root, path, making = making_staff(client, frozen)
    with held_for(7 * DAY):
        authorization = ann.authorized(f"{path}/authorization/patch", making)
        waiting = ann.call("PATCH", path, making, authorization)
        request_id = UUID(waiting.json()["approval_request_id"])
        held = row(authorization)
        maker = client.get(
            f"/approvals/requests/{request_id}", headers=root.headers
        ).json()["maker"]
        end_request(request_id, ending, maker, root.id)
        at_once = ann.call("PATCH", path, making, authorization)
        frozen.tick(timedelta(hours=20))
        later = ann.call("PATCH", path, making, authorization)

    problem(waiting, 403, "approval_pending")
    assert held["held_for"] == f"approvals.request:{request_id}"
    problem(at_once, 403, "action_not_authorized")
    problem(later, 403, "action_not_authorized")
    assert held_requests() == 1  # None submitted with it since.
    assert row(authorization)["consumed_at"] is None


def test_a_hold_ends_when_its_request_expires(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Three days, ``EXPIRES_AFTER``: the request's time is up, and the hold's."""
    ann, _, path, making = making_staff(client, frozen)
    with held_for(7 * DAY):
        authorization = ann.authorized(f"{path}/authorization/patch", making)
        waiting = ann.call("PATCH", path, making, authorization)
        frozen.tick(timedelta(days=3, minutes=10))
        late = ann.call("PATCH", path, making, authorization)

    problem(waiting, 403, "approval_pending")
    problem(late, 403, "action_not_authorized")
    assert held_requests() == 1


def test_a_hold_counts_only_what_approvals_says_of_its_own_requests(
    client: TestClient,
) -> None:
    """Another app's reference: no answer; a malformed or unknown request: over."""
    assert run_async(held_request_waits("billing.invoice:7")) is None
    assert run_async(held_request_waits("approvals.request:seven")) is False
    assert run_async(held_request_waits(f"approvals.request:{uuid4()}")) is False


def test_only_what_an_act_took_and_left_unspent_is_held() -> None:
    """Outside a request nothing is; within one, an act's untaken or spent stay."""
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
    request = "approvals.request:7"
    hold_authorizations(soon, held_for=request)  # No request: nothing to hold.
    kept = Kept()
    untaken = _Accepted(stored, "code", "POST /")
    spent = _Accepted(stored, "code", "POST /", taken=True, consumed=True)
    taken = _Accepted(stored, "code", "POST /", taken=True)
    kept.accepted = {"a": untaken, "b": spent, "c": taken}
    token = _kept.set(kept)
    try:
        hold_authorizations(later, held_for=request)
    finally:
        _kept.reset(token)

    assert (untaken.held, spent.held, taken.held) == (None, None, (later, request))


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


@pytest.mark.parametrize("held_for", ["", "approvals.request:" + "7" * 183])
def test_a_hold_names_what_it_waits_on_in_200_characters(held_for: str) -> None:
    """The column holds 200: a longer reference, or none, is refused at once."""
    with pytest.raises(ValueError, match="1 to 200 characters"):
        hold_authorizations(datetime(2027, 1, 5, tzinfo=UTC), held_for=held_for)


def test_without_auth_a_held_call_keeps_no_authorization(installed: None) -> None:
    """Approvals keeps the held call's authorization only where auth is installed."""
    from domainkit.contrib.approvals.services import _keep_authorizations

    _keep_authorizations(uuid4(), datetime(2027, 1, 5, tzinfo=UTC))  # Nothing kept.


def test_a_held_authorization_refuses_only_what_it_is_not_kept_for() -> None:
    """``refuse_held_elsewhere``: kept for one request, it refuses any other.

    Outside a request nothing is refused; an authorization held for
    nothing, untaken, or spent refuses nothing; one held for the request
    named passes, and one held for another refuses, the trail told at the
    end of the request (since 2.8, user report A17).
    """
    soon = datetime(2027, 1, 5, tzinfo=UTC)
    first, other = "approvals.request:1", "approvals.request:2"
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
        held_for=first,
    )
    refuse_held_elsewhere(other)  # No request: nothing to refuse.
    kept = Kept()
    held = _Accepted(stored, "code", "POST /", taken=True)
    kept.accepted = {
        "free": _Accepted(replace(stored, held_for=None), "code", "POST /", taken=True),
        "untaken": _Accepted(stored, "code", "POST /"),
        "spent": _Accepted(stored, "code", "POST /", taken=True, consumed=True),
    }
    token = _kept.set(kept)
    try:
        refuse_held_elsewhere(other)
        kept.accepted["held"] = held
        refuse_held_elsewhere(first)
        with pytest.raises(ActionNotAuthorized):
            refuse_held_elsewhere(None)
    finally:
        _kept.reset(token)

    assert held.unheard == "held_elsewhere"


def test_without_auth_a_held_call_is_refused_for_nothing(installed: None) -> None:
    """Approvals asks auth about the call's authorization only where it is installed."""
    from domainkit.contrib.approvals.services import _kept_for

    _kept_for(None)  # Nothing refused.
