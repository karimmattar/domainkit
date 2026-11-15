"""What ADR 0144's open questions turn on, shown on today's code (user report A6).

Each test pins one fact a question of the ADR rests on: how a rule is
asked, what a phone's step-up lets a session do, what the methods of a
step-up can tell apart, and what is kept of a phone's approval. They
describe the framework as it is; once action authorisations are built,
the tests of questions 3 to 5 change with it.
"""

import itertools
import json
from collections.abc import Callable
from contextvars import ContextVar
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from starlette.requests import HTTPConnection

from domainkit import clock
from domainkit.contrib.auth import AnyUser, Credentials, StepUp
from domainkit.contrib.auth.settings import StepUpSettings
from domainkit.contrib.ciba.api import HWK
from domainkit.contrib.ciba.tables import sign_in_requests
from domainkit.contrib.devices.tables import devices, signatures
from domainkit.db import connections
from domainkit.ddd import ConcurrentUpdate
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import BasePermission, HasPermissions, Principal, Rule
from domainkit.http.errors import install_exception_handlers
from domainkit.management.base import run_async
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import InMemoryGadgets, make_gadget
from domainkit_tests.test_auth_stepup import KNOWN, SteppedUpWhen, connection
from domainkit_tests.test_ciba import PHONE, App, Phone, app, start_push
from domainkit_tests.test_ciba_step_up import (
    FREE_TEXT_STEP_UP,
    approve_last,
    ask,
    redeem,
    web_of,
)
from domainkit_tests.test_resources import (
    LOCATION,
    RenameGadget,
    RenameIn,
    Users,
    as_,
    everyone,
    gadgets,
    rename,
)

__all__ = ["app"]  # The fixture the tests take.

pytestmark = pytest.mark.anyio


# Question 1: the rule asks, the act spends --------------------------------------


class CountedStepUp(StepUp):
    """``StepUp``, counting how often it is asked."""

    def __init__(self) -> None:
        """A step-up within five minutes; asked no time yet."""
        super().__init__(timedelta(minutes=5))
        self.asked = 0

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """Count, then answer as ``StepUp`` does."""
        self.asked += 1
        return await super().has_permission(request, user)


class SpendsWhenAsked(BasePermission):
    """The report's design: the rule spends the approval it is shown.

    ``objects`` says whether it is asked about objects as ``StepUp`` is,
    by asking itself again (``StepUp.has_object_permission``).
    """

    def __init__(self, approvals: set[str], *, objects: bool) -> None:
        """Spend from ``approvals``."""
        self.approvals = approvals
        self.objects = objects

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """Whether the request's approval was unspent; it is spent now."""
        approval = request.headers.get("x-approval")
        if approval not in self.approvals:
            return False
        self.approvals.discard(approval)
        return True

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """As ``StepUp``, asked again; or allowed, when not about objects."""
        return await self.has_permission(request, user) if self.objects else True


KEPT: ContextVar[str | None] = ContextVar("kept", default=None)
"""The approval a rule kept for the act, as the ADR's rule keeps it."""


class ChecksOnly(BasePermission):
    """The ADR's design: the rule checks the approval and keeps it for the act."""

    def __init__(self, approvals: set[str]) -> None:
        """Check against ``approvals``."""
        self.approvals = approvals

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """Whether the request's approval is unspent; it writes nothing."""
        approval = request.headers.get("x-approval")
        KEPT.set(approval)
        return approval in self.approvals

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """As ``has_permission``: asked twice, it answers twice the same."""
        return await self.has_permission(request, user)


type Rename = Callable[..., Any]


@pytest.fixture
def renaming(use_auth: UseAuth) -> Callable[..., Rename]:
    """``renaming(rule, service)``: a call that renames a gadget, as ``changer``.

    The gadget resource of the resource tests, its command route guarded
    by ``rule`` and served by ``service``: the framework's own routing.
    """
    use_auth()

    def make(rule: Rule, service: Callable[..., Any] = rename) -> Rename:
        store = InMemoryGadgets([make_gadget("a")])
        resource = gadgets(store)
        resource.command_route(
            RenameGadget, service=service, body=RenameIn, path="/rename", rule=rule
        )
        served = FastAPI()
        install_exception_handlers(served)
        served.include_router(resource)
        served.add_middleware(SteppedUpWhen)
        served.add_middleware(Users, users=everyone())
        client = TestClient(served)
        gadget = next(iter(store.items))

        def renamed(**headers: str) -> Any:  # noqa: ANN401 - a response.
            return client.post(
                f"{LOCATION}{gadget}/rename",
                json={"name": "b"},
                headers=as_("changer", **headers),
            )

        return renamed

    return make


def failing_once(spend: set[str] | None = None) -> Callable[..., Any]:
    """``rename``, losing a race the first time; spending the kept approval after."""
    calls: list[None] = []

    async def service(command: RenameGadget, uow: Any) -> Any:  # noqa: ANN401
        calls.append(None)
        if len(calls) == 1:
            raise ConcurrentUpdate("Someone else changed it first.")
        renamed = await rename(command, uow)
        if spend is not None:
            spend.discard(KEPT.get() or "")  # With the act, once it committed.
        return renamed

    return service


def test_one_request_asks_a_step_up_twice(renaming: Callable[..., Rename]) -> None:
    """The route asks the rule, then asks it about the gadget it loaded.

    ``StepUp.has_object_permission`` asks ``has_permission`` again, and a
    resource asks every rule that looks at objects (``reach.asks_objects``).
    A rule that wrote on being asked would write twice for one act.
    """
    rule = CountedStepUp()
    renamed = renaming(rule)

    answer = renamed(**{"X-Stepped-Up": "60"})

    assert answer.status_code == 200, answer.json()
    assert rule.asked == 2


def test_a_rule_that_spends_as_step_up_is_asked_refuses_its_own_act(
    renaming: Callable[..., Rename],
) -> None:
    """Spent at the route, the approval is gone when the gadget is checked."""
    approvals = {"approved"}
    renamed = renaming(SpendsWhenAsked(approvals, objects=True))

    answer = renamed(**{"X-Approval": "approved"})

    assert answer.status_code == 403, answer.json()
    assert approvals == set()  # Spent, and nothing done.


def test_a_rule_that_spends_loses_the_approval_when_the_act_then_fails(
    renaming: Callable[..., Rename],
) -> None:
    """Spent by the rule, a lost race needs a new approval from the phone."""
    approvals = {"approved"}
    renamed = renaming(SpendsWhenAsked(approvals, objects=False), failing_once())

    first = renamed(**{"X-Approval": "approved"})
    again = renamed(**{"X-Approval": "approved"})

    assert (first.status_code, first.json()["code"]) == (409, "concurrent_update")
    assert again.status_code == 403  # The user must approve again.


def test_an_approval_the_act_spends_survives_an_act_that_fails(
    renaming: Callable[..., Rename],
) -> None:
    """Checked by the rule, spent with the act: sent again, it works once."""
    approvals = {"approved"}
    renamed = renaming(ChecksOnly(approvals), failing_once(spend=approvals))

    first = renamed(**{"X-Approval": "approved"})
    again = renamed(**{"X-Approval": "approved"})
    third = renamed(**{"X-Approval": "approved"})

    assert first.status_code == 409
    assert again.status_code == 200, again.json()  # No new approval needed.
    assert third.status_code == 403  # Spent once, with the act.


def test_a_rule_that_spends_loses_the_approval_to_a_later_rules_refusal(
    renaming: Callable[..., Rename],
) -> None:
    """``&`` asks its rules in order: the spend happens before the refusal."""
    approvals = {"approved"}
    rule = SpendsWhenAsked(approvals, objects=False) & HasPermissions(
        "shop.delete_gadget"
    )
    renamed = renaming(rule)

    answer = renamed(**{"X-Approval": "approved"})

    assert answer.status_code == 403
    assert approvals == set()  # Spent on an act that never ran.


# Question 3: one approval of the page's words opens a window ----------------------


@FREE_TEXT_STEP_UP  # Deprecated in 2.4 for what this test shows.
def test_one_phone_approval_of_the_pages_words_opens_every_guarded_act(
    app: App,
) -> None:
    """The phone shows what the page wrote; the session then acts as it likes.

    ``/payees`` (``StepUp(max_age=5 minutes)``) passes again and again,
    from another machine with the same token too, until the window ends.
    """
    phone = Phone(app(PHONE))
    web = app()
    headers = web_of(web)

    started = ask(web, headers, "Show my balance").json()
    shown = approve_last(phone, started["match_code"])
    stepped = redeem(web, headers, started["request"])
    acts = [web.post("/payees", headers=headers).status_code for _ in range(3)]
    copied = app(("192.0.2.50", 40000)).post("/payees", headers=headers)
    with clock.freeze_time(clock.now() + timedelta(minutes=5, seconds=1)):
        later = web.post("/payees", headers=headers)

    assert shown["binding_message"] == "Show my balance"  # All the phone showed.
    assert stepped.status_code == 200, stepped.json()
    assert acts == [204, 204, 204]  # An act the phone never named, three times.
    assert copied.status_code == 204  # The token alone, from elsewhere.
    assert (later.status_code, later.json()["code"]) == (401, "step_up_required")


# Question 4: a step-up's methods cannot tell the phone apart ----------------------

PHONE_PROVES = HWK
"""What the phone's step-up proves: ``amr`` ``["hwk"]`` (``test_ciba_step_up``)."""
PASSKEY_PROVES = frozenset({"hwk", "mfa"})
"""What a passkey's step-up proves (``test_devices``, ``amr`` ``["hwk", "mfa"]``)."""
CODE_PROVES = frozenset({"otp"})
"""What a code of the authenticator app proves."""


async def passes(rule: StepUp, methods: frozenset[str]) -> bool:
    """Whether ``rule`` takes a step-up made just now proving ``methods``."""
    credentials = Credentials(KNOWN, scheme="jwt", auth_time=clock.now(), amr=methods)
    return await rule.has_permission(connection(credentials), KNOWN)


async def test_no_step_up_takes_the_phone_and_refuses_a_passkey_or_a_code(
    use_auth: UseAuth,
) -> None:
    """Every rule that can be built takes a passkey where it takes the phone.

    And every rule ``check`` can let through, asking only what
    ``STEP_UP.METHODS`` may hold (``auth.E106`` refuses the others), takes
    a code of the authenticator app too: a route cannot ask for the phone.
    """
    use_auth()
    built = []
    for size in range(1, 5):
        for methods in itertools.combinations(("pwd", "otp", "hwk", "mfa"), size):
            try:
                built.append(StepUp(timedelta(minutes=5), methods=methods))  # type: ignore[arg-type]
            except ImproperlyConfigured:
                continue  # Without "otp", or repeating one.
    checked = [rule for rule in built if rule.required() <= {"pwd", "otp"}]

    assert len(built) == 8  # Every set holding "otp".
    assert [sorted(rule.required()) for rule in checked] == [["otp"], ["otp", "pwd"]]
    for rule in built:
        if await passes(rule, PHONE_PROVES):
            assert await passes(rule, PASSKEY_PROVES), rule.required()
    for rule in checked:
        if await passes(rule, PHONE_PROVES):
            assert await passes(rule, CODE_PROVES), rule.required()


def test_neither_a_rule_nor_the_settings_can_name_the_phone_alone() -> None:
    """``hwk`` alone is refused, and ``STEP_UP.METHODS`` takes ``pwd`` and ``otp``."""
    with pytest.raises(ImproperlyConfigured, match="METHODS must hold"):
        StepUp(timedelta(minutes=5), methods=("hwk",))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        StepUpSettings(METHODS=("otp", "hwk"))


# Question 5: the phone's signature is checked, then dropped ------------------------


def stored(table: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - a Table.
    """Every row of ``table``."""

    async def read() -> list[dict[str, Any]]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(select(table))
            return [dict(row._mapping) for row in found]

    return run_async(read())


def test_the_phone_signs_its_approval_and_nothing_keeps_the_signature(
    app: App,
) -> None:
    """Unsigned, the approval is refused; signed, only the device's id stays.

    The signature is in no row: ``domainkit_device_signatures`` keeps the
    signed message's digest, for replays, and the request the device's
    id. Unbinding the device deletes its key, so no approval it made can
    be checked again.
    """
    web = app(NUMBER_MATCHING=False)
    phone = Phone(app(PHONE))
    start_push(web)
    [waiting] = phone.waiting()
    path = f"/auth/app-sign-in/requests/{waiting['id']}/approve"

    unsigned = phone.client.post(path, json={}, headers=phone.headers)
    body = json.dumps({}).encode()
    moment = int(clock.now().timestamp() * 1000) + 1_000
    signing = phone.device.headers("POST", path, body, when=moment)
    signed = phone.client.post(
        path,
        content=body,
        headers={**phone.headers, "Content-Type": "application/json", **signing},
    )
    [request] = stored(sign_in_requests)
    kept = repr([request, *stored(signatures)])
    device_id = UUID(phone.device.id)
    unbound = phone.send("DELETE", f"/auth/devices/{device_id}")

    assert unsigned.status_code in {401, 403}, unsigned.json()
    assert signed.status_code == 204, signed.json()
    assert request["approved_at"] is not None
    assert request["device_id"] == device_id
    assert signing["X-Device-Signature"] not in kept
    assert not [c.name for c in sign_in_requests.columns if "signature" in c.name]
    assert unbound.status_code == 204, unbound.json()
    assert [row for row in stored(devices) if row["id"] == device_id] == []
