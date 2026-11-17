"""What a person typed reaches a phone whole, and holding no link if asked (A17).

A stolen session writes what a screen shows of the act it asks for: an
impersonation's reason, a group's new name, a user's names, a rejection's
reason, a note a guard shows. With ``AUTH.AUTHORIZATIONS.TYPED_LINKS`` set
to ``"refused"``, each twin refuses such text that holds a link, an email
address, or a number to call (``422 link_not_shown``), before an
authorization is stored, as ``WHOLE`` refuses what does not fit. Rejecting
and cancelling check their own reason, never what the request typed, and
with ``WHOLE`` on refuse a reason longer than a line (``422
too_much_to_show``), as an impersonation's reason is (ADR 0180).
"""

from collections.abc import Iterator
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.approvals import ApprovalsUnitOfWork, Policy, guard
from domainkit.contrib.approvals.builtin import show_about_user
from domainkit.contrib.auth.screens import Typed
from domainkit.ddd import Command
from domainkit.http import get_asgi_application
from domainkit.management.base import run_async
from domainkit_tests.conftest import Migrated
from domainkit_tests.sample_admin_action_routes import TRANSFER, Transfer, transfer
from domainkit_tests.support.auth import create_user
from domainkit_tests.test_admin_actions import (
    ACTS,
    Staff,
    end_of,
    held_requests,
    problem,
    stored,
)
from domainkit_tests.test_admin_actions import frozen as frozen  # The fixture.
from domainkit_tests.test_approval_screens import SCREENS, fingerprint_of, pairs
from domainkit_tests.test_approvals_request_actions import (
    EVERY_STEP,
    PAYMENT,
    TREASURERS,
    Person,
)
from domainkit_tests.test_auth_admin import make_group

REFUSED: Final = {**ACTS.settings["AUTH"], "AUTHORIZATIONS": {"TYPED_LINKS": "refused"}}
"""``AUTH`` with typed text that holds a link refused."""
WHOLE: Final = {**ACTS.settings["AUTH"], "AUTHORIZATIONS": {"WHOLE": True}}
"""``AUTH`` with every act shown whole, or refused."""
CALL: Final = "Locked out? Call +1 555 010 0100 to confirm"
API: Final = "/approvals/requests"


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The admin API's changes acts; adding groups and impersonating held."""
    with migrated(SCREENS), TestClient(get_asgi_application()) as test_client:
        yield test_client


@pytest.fixture
def steps(migrated: Migrated) -> Iterator[TestClient]:
    """Every step of a request an act; a transfer's memo shown as its maker typed it."""

    def shown(command: Transfer) -> list[tuple[str, object]]:
        return [("Payee", command.payee), ("Memo", Typed(command.memo))]

    with migrated(EVERY_STEP), TestClient(get_asgi_application()) as test_client:
        guard(
            TRANSFER,
            transfer,
            unit_of_work=ApprovalsUnitOfWork,
            policies=[Policy("tests.typed_transfers", group=TREASURERS, approvals=2)],
            show=shown,
            domain="alpha",
        )
        make_group(TREASURERS)
        yield test_client


def test_staff_typing_a_link_is_refused_at_the_admin_twin(
    client: TestClient, frozen: FrozenClock
) -> None:
    """A reason, a group's new name, a user's names: refused before anything is stored.

    An address or a number set as the user's own (``email``) is what the
    change sets, never typed text; and a name stored with a link never
    locks what it names: renaming that group away from it is shown. Off,
    the default, each is shown as typed, as in 2.7.
    """
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target")
    readers = make_group("readers")
    linked = make_group("call 555 010 0100")
    asks = [
        (f"/users/{target}/impersonate/authorization", {"reason": CALL}),
        ("/groups/authorization", {"name": "evil.example.com"}),
        (f"/groups/{readers}/authorization/patch", {"name": "visit evil[.]com"}),
        (
            "/users/authorization",
            {"username": "new", "fields": {"first_name": "ann\uff20evil.example"}},
        ),
        (f"/users/{target}/authorization/patch", {"fields": {"last_name": "www.x"}}),
    ]

    with override_settings(AUTH=REFUSED):
        before = len(stored())
        refused = [root.ask(path, body) for path, body in asks]
        unasked = len(stored()) - before
        email = root.ask(
            f"/users/{target}/authorization/patch",
            {"fields": {"email": "new@example.com"}},
        )
        renaming = root.ask(f"/groups/{linked}/authorization/patch", {"name": "ok"})
        reason = root.ask(
            f"/users/{target}/impersonate/authorization", {"reason": "INC0012345"}
        )
    shown = [root.ask(path, body) for path, body in asks]

    for response in refused:
        problem(response, 422, "link_not_shown")
    assert unasked == 0
    assert ("Email", "new@example.com") in pairs(email)
    assert pairs(renaming) == [
        ("Group", f"call 555 010 0100 ({end_of(linked)})"),
        ("New name", "ok"),
    ]
    assert ("Reason", "INC0012345") in pairs(reason)
    assert ("Reason", CALL) in pairs(shown[0])
    assert all(response.status_code == 201 for response in shown)


def test_without_the_act_a_group_is_never_named_with_a_link(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Its name reaches approvers' screens later: refused where staff type it."""
    root = Staff(client, frozen, "root", superuser=True)
    readers = make_group("readers")

    with override_settings(AUTH={**REFUSED, "ADMIN_ACTION": False}):
        created = root.call("POST", "/groups", {"name": "evil.example.com"})
        renamed = root.call("PATCH", f"/groups/{readers}", {"name": "call 5550100123"})
        named = root.call("POST", "/groups", {"name": "auditors"})

    problem(created, 422, "link_not_shown")
    problem(renamed, 422, "link_not_shown")
    assert named.status_code == 201, named.json()


def test_a_held_impersonation_whose_reason_holds_a_link_is_never_submitted(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Without the admin API's act, its approver's screen refuses it when it is held.

    The request would wait for an approval nobody could give: it is
    refused before it is stored, as one that does not fit whole is.
    """
    boss = Staff(client, frozen, "boss", superuser=True)
    target = create_user("target")
    path = f"/users/{target}/impersonate"

    with override_settings(AUTH={**REFUSED, "ADMIN_ACTION": False}):
        refused = boss.call("POST", path, {"reason": CALL})
        held = boss.call("POST", path, {"reason": "Ticket 42"})

    problem(refused, 422, "link_not_shown")
    problem(held, 403, "approval_pending")
    assert held_requests() == 1


def test_what_a_guard_marks_typed_is_refused_where_it_is_typed(
    steps: TestClient, frozen: FrozenClock
) -> None:
    """Submitting it and approving it are refused; rejecting and cancelling never.

    The request was stored before the setting was on. Its approver may
    not approve it, as one they could not see whole; they reject it, for
    a reason of their own, which is checked, and its maker may withdraw it.
    """
    maker = Person(steps, frozen, "maker", treasurer=False)
    ann = Person(steps, frozen, "ann", treasurer=True)
    request = maker.submit(memo=CALL)
    body = {"action": TRANSFER, "arguments": {**PAYMENT, "memo": CALL}}
    rejecting = f"/{request['id']}/reject/authorization"
    cancelling = f"/{request['id']}/cancel/authorization"

    with override_settings(AUTH=REFUSED):
        fingerprint = {"fingerprint": request["fingerprint"]}
        submitting = maker.post("/authorization", body)
        approving = ann.post(f"/{request['id']}/approve/authorization", fingerprint)
        rejected = ann.post(rejecting, {"reason": "No: see evil.example"})
        stopped = ann.post(rejecting, {"reason": "Wrong payee"})
        withdrawn = maker.post(cancelling, {"reason": "mail ann@example.com"})
        cancelled = maker.post(cancelling, {})

    problem(submitting, 422, "link_not_shown")
    problem(approving, 422, "link_not_shown")
    problem(rejected, 422, "link_not_shown")
    problem(withdrawn, 422, "link_not_shown")
    assert pairs(stopped) == [
        ("Action", TRANSFER),
        ("Reason", "Wrong payee"),
        ("Payee", "Ada Lovelace"),
        ("Memo", CALL),
    ]
    assert pairs(cancelled)[-1] == ("Memo", CALL)


def test_with_whole_on_a_reason_past_a_line_is_refused_at_its_twin(
    steps: TestClient, frozen: FrozenClock
) -> None:
    """Rejecting and cancelling are refused for their own reason, as it shows.

    A reason of 64 characters is a line; one more, or a new line that
    shows as two characters, is refused, and no authorization is stored.
    Off, the reason is cut, as in 2.7, though the maker reads it whole.
    """
    maker = Person(steps, frozen, "maker", treasurer=False)
    ann = Person(steps, frozen, "ann", treasurer=True)
    request = maker.submit()
    rejecting = f"/{request['id']}/reject/authorization"
    cancelling = f"/{request['id']}/cancel/authorization"
    long, line, broken = "x" * 65, "y" * 64, "z" * 63 + "\n"

    with override_settings(AUTH=WHOLE):
        before = len(stored())
        refused = [
            ann.post(rejecting, {"reason": long}),
            ann.post(rejecting, {"reason": broken}),
            maker.post(cancelling, {"reason": long}),
        ]
        unasked = len(stored()) - before
        whole = ann.post(rejecting, {"reason": line})
        withdrawing = maker.post(cancelling, {"reason": line})
    cut = ann.post(rejecting, {"reason": long})

    for response in refused:
        problem(response, 422, "too_much_to_show")
    assert unasked == 0
    assert pairs(whole)[1] == ("Reason", line)
    assert pairs(withdrawing)[1] == ("Reason", line)
    assert pairs(cut)[1] == ("Reason", "x" * 63 + "…")


def reject_twin(person: Any, request_id: str, reason: str) -> Any:  # noqa: ANN401 - responses.
    """``person`` asks to reject ``request_id`` for ``reason``."""
    return person.client.post(
        f"{API}/{request_id}/reject/authorization",
        json={"reason": reason},
        headers=person.headers,
    )


def test_an_approver_rejects_a_held_impersonation_whose_reason_holds_a_link(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The stated reason is the maker's: rejecting shows it, never refused for it.

    Approving it is refused, with ``TYPED_LINKS`` refused since it was
    held; the approver's own reason is checked.
    """
    root = Staff(client, frozen, "root", superuser=True)
    boss = Staff(client, frozen, "boss", superuser=True)
    target = create_user("target")
    path = f"/users/{target}/impersonate"
    authorization = root.authorized(f"{path}/authorization", {"reason": CALL})
    waiting = root.call("POST", path, {"reason": CALL}, authorization)
    request_id = waiting.json()["approval_request_id"]
    fingerprint = fingerprint_of(boss, request_id)

    with override_settings(AUTH=REFUSED):
        approving = boss.client.post(
            f"{API}/{request_id}/approve/authorization",
            json={"fingerprint": fingerprint},
            headers=boss.headers,
        )
        rejecting = reject_twin(boss, request_id, "No")
        linked = reject_twin(boss, request_id, "Call me on 555 010 0100")

    problem(approving, 422, "link_not_shown")
    assert pairs(rejecting) == [
        ("Action", "auth.impersonate"),
        ("Reason", "No"),
        ("User", f"target ({end_of(target)})"),
        ("Stated reason", CALL),
    ]
    problem(linked, 422, "link_not_shown")


class Stated(Command):
    """A command about a user whose reason may be left out."""

    user_id: UUID
    reason: str | None = None


def test_a_reason_about_a_user_is_shown_as_its_maker_typed_it(
    client: TestClient,
) -> None:
    """A text is ``Typed``, checked where a screen shows it; none shows as given."""
    target = create_user("target")

    typed = run_async(show_about_user(Stated(user_id=target, reason="Ticket 42")))
    none = run_async(show_about_user(Stated(user_id=target)))

    assert typed[1] == ("Stated reason", "Ticket 42")
    assert isinstance(typed[1][1], Typed)
    assert none[1] == ("Stated reason", None)
