"""What an approver's phone shows of a request: whole, the user named (user report A23).

Approving a held grant of the admin API with ``APPROVE_ACTION`` showed
``user_id`` and the grant's list as one line of JSON, cut at 64 characters
with no count: three of thirty permissions, and a user by id. A guard's
``show`` now names what its request shows ("User: target"); a list goes
over as many lines as it takes, and past the screen says how many more.
With ``AUTH.AUTHORIZATIONS.WHOLE`` on, a request no approver's screen
shows whole is refused when it is submitted, and approving one is refused;
rejecting never is (ADR 0173).
"""

from collections.abc import Iterator
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.approvals import ApprovalsUnitOfWork, Policy, requires_approval
from domainkit.contrib.approvals.authorizing import shown_of
from domainkit.contrib.approvals.builtin import show_about_user
from domainkit.contrib.approvals.guards import Guard
from domainkit.ddd import Command
from domainkit.http import get_asgi_application
from domainkit.i18n import override
from domainkit.management.base import run_async
from domainkit_tests.conftest import Migrated, Shape
from domainkit_tests.support.auth import create_user
from domainkit_tests.test_admin_actions import (
    ACTS,
    Staff,
    held_requests,
    problem,
    stored,
)
from domainkit_tests.test_admin_actions import frozen as frozen  # The fixture.
from domainkit_tests.test_auth_admin import make_group

SCREENS: Final = Shape(
    "approval_screens",
    "approvals",
    {
        **ACTS.settings,
        "APPROVALS": {
            **ACTS.settings["APPROVALS"],
            "APPROVE_ACTION": True,
            "ADMIN_APPROVALS": {"auth.set_groups": 1, "auth.impersonate": 1},
        },
    },
)
"""Every admin change an act, and every step of a request; adding groups and
impersonating wait for a superuser's approval."""
WHOLE: Final = {**ACTS.settings["AUTH"], "AUTHORIZATIONS": {"WHOLE": True}}
"""``AUTH`` with ``AUTHORIZATIONS.WHOLE`` on."""
API: Final = "/approvals/requests"
LONG_NAMES: Final = tuple(f"finance_reporting_readers_{each:02}" for each in range(30))
"""Thirty groups whose names fit two to a line."""


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The project's API: admin grants held, every step of a request an act."""
    with migrated(SCREENS), TestClient(get_asgi_application()) as test_client:
        yield test_client


def pairs(response: Any) -> list[tuple[str, str]]:  # noqa: ANN401 - a response.
    """The labels and values ``response``'s screen shows."""
    assert response.status_code == 201, response.json()
    shown = response.json()["shown"]
    return [(each["label"], each["value"]) for each in shown["details"]]


def held(maker: Staff, target: str, names: tuple[str, ...]) -> Any:  # noqa: ANN401 - a response.
    """``maker``'s authorized call that adds ``target`` to ``names``."""
    body = {"groups": list(names)}
    authorization = maker.authorized(f"/users/{target}/groups/authorization", body)
    return maker.call("PUT", f"/users/{target}/groups", body, authorization)


def ask_to(person: Staff, request_id: str, step: str, body: dict[str, Any]) -> Any:  # noqa: ANN401 - a response.
    """``person`` asks to approve or reject (``step``) a request."""
    return person.client.post(
        f"{API}/{request_id}/{step}/authorization", json=body, headers=person.headers
    )


def fingerprint_of(person: Staff, request_id: str) -> str:
    """The request's fingerprint, as ``person`` reads it."""
    read = person.client.get(f"{API}/{request_id}", headers=person.headers)
    assert read.status_code == 200, read.json()
    found: str = read.json()["fingerprint"]
    return found


def test_an_approver_sees_the_user_by_name_and_every_group_added(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The user by name and id, and each group, on as many lines as needed."""
    root = Staff(client, frozen, "root", superuser=True)
    boss = Staff(client, frozen, "boss", superuser=True)
    target = create_user("target")
    names = (*(f"aaa_harmless_{each}" for each in range(5)), "zz_admins")
    for name in names:
        make_group(name)

    request_id = held(root, str(target), names).json()["approval_request_id"]
    fingerprint = fingerprint_of(boss, request_id)
    asked = ask_to(boss, request_id, "approve", {"fingerprint": fingerprint})
    with override("ar"):
        arabic = ask_to(boss, request_id, "approve", {"fingerprint": fingerprint})

    assert pairs(asked) == [
        ("Action", "auth.set_groups"),
        ("User", f"target ({target})"),
        ("Adds", "aaa_harmless_0, aaa_harmless_1, aaa_harmless_2, aaa_harmless_3"),
        ("Adds (2)", "aaa_harmless_4, zz_admins"),
    ]
    assert [label for label, _ in pairs(arabic)][1:] == [
        "User",
        "Adds",
        "Adds (2)",
    ]


def test_an_impersonation_shows_who_and_why(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The user, and the reason stated; rejecting shows its own reason beside it."""
    root = Staff(client, frozen, "root", superuser=True)
    boss = Staff(client, frozen, "boss", superuser=True)
    target = create_user("target")
    body = {"reason": "Ticket 42"}
    path = f"/users/{target}/impersonate"

    authorization = root.authorized(f"{path}/authorization", body)
    waiting = root.call("POST", path, body, authorization)
    request_id = waiting.json()["approval_request_id"]
    fingerprint = fingerprint_of(boss, request_id)
    asked = ask_to(boss, request_id, "approve", {"fingerprint": fingerprint})
    rejecting = ask_to(boss, request_id, "reject", {"reason": "Not this one"})

    assert pairs(asked) == [
        ("Action", "auth.impersonate"),
        ("User", f"target ({target})"),
        ("Stated reason", "Ticket 42"),
    ]
    assert pairs(rejecting) == [
        ("Action", "auth.impersonate"),
        ("Reason", "Not this one"),
        ("User", f"target ({target})"),
        ("Stated reason", "Ticket 42"),
    ]


def test_a_grant_longer_than_the_screen_says_how_many_more(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Off ``WHOLE``, thirty groups: the first ones shown, and the count.

    With ``WHOLE`` on, approving that request is refused, as it cannot be
    shown whole; rejecting it never is: stopping a request does nothing it
    does not show.
    """
    root = Staff(client, frozen, "root", superuser=True)
    boss = Staff(client, frozen, "boss", superuser=True)
    target = create_user("target")
    for name in LONG_NAMES:
        make_group(name)

    with override_settings(AUTH={**ACTS.settings["AUTH"], "ADMIN_ACTION": False}):
        waiting = boss.call("PUT", f"/users/{target}/groups", {"groups": LONG_NAMES})
    request_id = waiting.json()["approval_request_id"]
    fingerprint = fingerprint_of(root, request_id)
    counted = pairs(ask_to(root, request_id, "approve", {"fingerprint": fingerprint}))
    with override_settings(AUTH=WHOLE):
        approving = ask_to(root, request_id, "approve", {"fingerprint": fingerprint})
        rejecting = ask_to(root, request_id, "reject", {"reason": "Too many"})

    assert [label for label, _ in counted] == [
        "Action",
        "User",
        "Adds",
        "Adds (2)",
        "Adds (3)",
        "Adds (4)",
    ]
    assert counted[-1][1] == f"{LONG_NAMES[6]} and 23 more"
    assert all(len(value) <= 64 for _, value in counted)
    problem(approving, 422, "too_much_to_show")
    assert [label for label, _ in pairs(rejecting)][:4] == [
        "Action",
        "Reason",
        "User",
        "Adds",
    ]
    assert pairs(rejecting)[-1][1].endswith(" more")


def test_with_whole_on_a_grant_no_approver_could_see_whole_is_never_submitted(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The maker's screen holds the change; the approver's, a line longer, does not.

    Ten groups take five lines: the maker's screen would show the user and
    them. The approver's would show the action too, seven lines: the
    maker's twin refuses the change before an authorization is stored.
    Without the act (``ADMIN_ACTION`` off), the change is refused when it
    is submitted. Either way no request is stored; eight groups wait.
    """
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target")
    names = LONG_NAMES[:10]
    for name in names:
        make_group(name)
    body = {"groups": list(names)}

    with override_settings(AUTH=WHOLE):
        before = len(stored())
        asked = root.ask(f"/users/{target}/groups/authorization", body)
        unasked = len(stored()) - before
        fewer = held(root, str(target), names[:8])
    with override_settings(AUTH={**WHOLE, "ADMIN_ACTION": False}):
        submitted = root.call("PUT", f"/users/{target}/groups", body)

    problem(asked, 422, "too_much_to_show")
    assert unasked == 0
    problem(submitted, 422, "too_much_to_show")
    problem(fewer, 403, "approval_pending")
    assert held_requests() == 1


def test_with_whole_on_a_long_name_never_locks_a_user_or_a_group(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The name is cut beside the id, which the act binds: never too much to show.

    A user whose username has 150 characters is deactivated, deleted,
    logged out, given a password; a group whose name has 150 is renamed
    and deleted; a held grant to that user is approved: each screen shows
    the name cut and the id whole (review of ADR 0173).
    """
    root = Staff(client, frozen, "root", superuser=True)
    boss = Staff(client, frozen, "boss", superuser=True)
    long = "u" * 150
    target = create_user(long)
    group = make_group("g" * 150)
    make_group("readers")
    user_line = ("User", f"{'u' * 24}… ({target})")
    group_line = ("Group", f"{'g' * 24}… ({group})")

    with override_settings(AUTH=WHOLE):
        asked = [
            root.ask(path, body)
            for path, body in (
                (f"/users/{target}/authorization/patch", {"is_active": False}),
                (f"/users/{target}/authorization/delete", None),
                (f"/users/{target}/logout/authorization", None),
                (f"/users/{target}/password/authorization", {"password": None}),
            )
        ]
        renaming = root.ask(f"/groups/{group}/authorization/patch", {"name": "short"})
        deleting = root.ask(f"/groups/{group}/authorization/delete")
        request_id = held(root, str(target), ("readers",)).json()["approval_request_id"]
        fingerprint = fingerprint_of(boss, request_id)
        approving = ask_to(boss, request_id, "approve", {"fingerprint": fingerprint})

    for response in asked:
        assert pairs(response)[0] == user_line
    assert pairs(renaming) == [group_line, ("New name", "short")]
    assert pairs(deleting) == [group_line]
    assert pairs(approving) == [
        ("Action", "auth.set_groups"),
        user_line,
        ("Adds", "readers"),
    ]


@requires_approval("tests.screens")
async def act(command: "Pay", uow: ApprovalsUnitOfWork) -> None:
    """Nothing: a guard's service, for its ``show``."""


class Pay(Command):
    """Pay ``amount`` to ``payee``."""

    payee: str
    amount: str


def test_a_guard_names_what_its_request_shows() -> None:
    """A plain or a coroutine function of the command; without one, its arguments."""

    def plain(command: Pay) -> dict[str, object]:
        return {"Pay": f"{command.amount} to {command.payee}"}

    async def awaited(command: Pay) -> list[tuple[str, object]]:
        return [("Payee", command.payee)]

    def guarded(show: Any) -> Guard:  # noqa: ANN401 - a show, or None.
        policy = Policy("tests.screens", approvals=1, permission="tests.approve")
        return Guard(
            "tests.screens", Pay, act, ApprovalsUnitOfWork, (policy,), show=show
        )

    command = Pay(payee="Ada", amount="10.00")

    async def shown() -> list[list[tuple[str, object]]]:
        return [
            list(await shown_of(guarded(each), command))
            for each in (plain, awaited, None)
        ]

    assert run_async(shown()) == [
        [("Pay", "10.00 to Ada")],
        [("Payee", "Ada")],
        [("payee", "Ada"), ("amount", "10.00")],
    ]


class Note(Command):
    """A command about a user with a field the admin API's commands have not."""

    user_id: UUID
    removed: tuple[str, ...]
    note: str


def test_a_request_about_a_user_gone_shows_their_id_and_every_field(
    client: TestClient,
) -> None:
    """Erased meanwhile: shown by id, never refused; other fields by their name."""
    gone = uuid4()

    shown = run_async(show_about_user(Note(user_id=gone, removed=(), note="n")))

    assert shown == [("User", f"erased ({gone})"), ("note", "n")]
