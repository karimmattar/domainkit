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
from pydantic import JsonValue

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.approvals import ApprovalsUnitOfWork, Policy, guard
from domainkit.contrib.approvals.builtin import (
    SetStaff,
    show_about_user,
    show_set_staff,
)
from domainkit.contrib.auth.admin_actions import ADMIN_CHANGE, AdminAct
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
    row,
    stored,
)
from domainkit_tests.test_admin_actions import frozen as frozen  # The fixture.
from domainkit_tests.test_admin_acts_bind_groups import pending, stored_requests
from domainkit_tests.test_approval_screens import (
    SCREENS,
    ask_to,
    end4,
    fingerprint_of,
    held,
    pairs,
)
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
    """Its name reaches approvers' screens later: refused where staff type it.

    As a screen may show it: cut past its twentieth character.
    """
    root = Staff(client, frozen, "root", superuser=True)
    readers = make_group("readers")

    with override_settings(AUTH={**REFUSED, "ADMIN_ACTION": False}):
        created = root.call("POST", "/groups", {"name": "evil.example.com"})
        renamed = root.call("PATCH", f"/groups/{readers}", {"name": "call 5550100123"})
        cut = root.call(
            "POST", "/groups", {"name": "a team for the vendor evil.corporate"}
        )
        named = root.call("POST", "/groups", {"name": "auditors"})

    problem(created, 422, "link_not_shown")
    problem(renamed, 422, "link_not_shown")
    problem(cut, 422, "link_not_shown")  # Shown cut, as "...vendor evil.co…".
    assert named.status_code == 201, named.json()


def test_a_group_whose_stored_name_holds_a_link_is_refused_where_it_is_granted(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Named before the setting: refused where it is granted until it is renamed.

    The admin API's act and approvals' screens show a group added in a few
    words (``group_item``): its name is text a person typed, read as the
    item shows it; its id and counts are not. Rejecting a request held
    before shows it, never refused for it; without the act, holding one is
    refused (user report A17).
    """
    root = Staff(client, frozen, "root", superuser=True)
    boss = Staff(client, frozen, "boss", superuser=True)
    target, other = str(create_user("target")), str(create_user("other"))
    linked = make_group("visit evil.example")
    body = {"groups": ["visit evil.example"]}
    waiting = held(root, target, ("visit evil.example",))
    request_id = waiting.json()["approval_request_id"]
    fingerprint = {"fingerprint": fingerprint_of(boss, request_id)}
    renaming = f"/groups/{linked}"

    with override_settings(AUTH=REFUSED):
        asked = root.ask(f"/users/{target}/groups/authorization", body)
        approving = ask_to(boss, request_id, "approve", fingerprint)
        rejecting = ask_to(boss, request_id, "reject", {"reason": "No"})
        with override_settings(AUTH={**REFUSED, "ADMIN_ACTION": False}):
            submitted = root.call("PUT", f"/users/{other}/groups", body)
        authorization = root.authorized(
            f"{renaming}/authorization/patch", {"name": "auditors"}
        )
        renamed = root.call("PATCH", renaming, {"name": "auditors"}, authorization)
        approvable = ask_to(boss, request_id, "approve", fingerprint)
        granting = root.ask(
            f"/users/{target}/groups/authorization", {"groups": ["auditors"]}
        )

    problem(asked, 422, "link_not_shown")
    problem(approving, 422, "link_not_shown")
    assert ("Adds", f"visit evil.example ({end4(linked)}) 0 perms") in pairs(rejecting)
    problem(submitted, 422, "link_not_shown")
    assert renamed.status_code == 200, renamed.json()
    assert ("Adds", f"auditors ({end4(linked)}) 0 perms") in pairs(approvable)
    assert ("Adds", f"auditors ({end4(linked)}) 0 perms") in pairs(granting)


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


def test_a_cut_reason_or_name_is_checked_as_the_phone_shows_it(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Off ``WHOLE``, the phone shows 63 characters and ``…``: a link may begin there.

    ``evil.corporate`` names no domain, ``evil.co…`` does; a number's
    letters cut off leave a number to call. A cut inside an address or a
    link is refused as the whole is (review of ADR 0180).
    """
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target")
    domain = "Approved; details at our partner evil.co".rjust(63, "x") + "rporate"
    number = "For any question please ring 555 0100 123".rjust(63, "x") + "abc"
    address = "Write to ann@example.com".rjust(70, "x")
    link = "See https://evil.example/x".rjust(70, "x")
    impersonate = f"/users/{target}/impersonate/authorization"
    patch = f"/users/{target}/authorization/patch"

    shown = root.ask(impersonate, {"reason": domain})
    with override_settings(AUTH=REFUSED):
        refused = [
            root.ask(impersonate, {"reason": domain}),
            root.ask(patch, {"fields": {"first_name": number}}),
            root.ask(impersonate, {"reason": address}),
            root.ask(impersonate, {"reason": link}),
        ]

    assert ("Reason", domain[:63] + "…") in pairs(shown)
    for response in refused:
        problem(response, 422, "link_not_shown")


def test_a_form_saved_whole_leaves_a_field_it_does_not_change_off_the_screen(
    client: TestClient, frozen: FrozenClock
) -> None:
    """A text sent as it is stored changes nothing: never checked, and not shown.

    With ``TYPED_LINKS`` refused, a stored link never reaches the phone
    through a change that only sends it again, and a change that sets one
    beside it is refused. ``"shown"`` keeps 2.7's screen, every field
    sent. The act binds every field sent, as in 2.7, and an address sent
    unchanged, an identifier, still shows (review of ADR 0180).
    """
    root = Staff(client, frozen, "root", superuser=True)
    stored_name = "call +1 555 0100 123"
    target = create_user(
        "target", first_name=stored_name, last_name="Smith", email="t@example.com"
    )
    patch = f"/users/{target}/authorization/patch"
    user_line = ("User", f"target ({end_of(target)})")
    whole_form = {"fields": {"first_name": stored_name, "last_name": "Jones"}}

    as_in_2_7 = root.ask(patch, whole_form)
    with override_settings(AUTH=REFUSED):
        alone = root.ask(patch, {"fields": {"first_name": stored_name}})
        saved = root.ask(
            patch, {"fields": {"first_name": stored_name, "last_name": "Jones"}}
        )
        beside = root.ask(
            patch,
            {"fields": {"first_name": stored_name, "last_name": "visit evil.example"}},
        )
        changed = root.ask(
            patch, {"fields": {"first_name": "www.evil", "last_name": "Smith"}}
        )
        address = root.ask(patch, {"fields": {"email": "t@example.com"}})

    assert pairs(as_in_2_7) == [
        user_line,
        ("First name", stored_name),
        ("Last name", "Jones"),
    ]
    assert pairs(alone) == [user_line]
    assert pairs(saved) == [user_line, ("Last name", "Jones")]
    problem(beside, 422, "link_not_shown")
    problem(changed, 422, "link_not_shown")
    assert pairs(address) == [user_line, ("Email", "t@example.com")]
    bound = AdminAct(kind="update", user_id=target, values={"first_name": stored_name})
    assert row(alone.json()["id"])["digest"] == ADMIN_CHANGE.digest_of(bound)


def test_a_held_grants_fields_are_typed_but_its_identifiers_and_flags(
    client: TestClient,
) -> None:
    """Making staff, or a return, shows the fields sent with it: text staff typed.

    An address, a number, and a username identify the user, as on the
    admin API's screen; a flag is no text (review of ADR 0180).
    """
    target = create_user("target")
    values: dict[str, JsonValue] = {
        "first_name": "Ann",
        "email": "a@example.com",
        "is_active": True,
    }

    shown = run_async(show_set_staff(SetStaff(user_id=target, values=values)))

    typed = {label: isinstance(value, Typed) for label, value in shown[1:]}
    assert typed == {"Active": False, "Email": False, "First name": True}


def test_a_held_grants_fields_are_checked_where_its_approver_sees_them(
    steps: TestClient, frozen: FrozenClock
) -> None:
    """Held without the admin API's act, a name holding a link is refused when held.

    One held before the setting cannot be approved. A name sent as it is
    stored changes nothing: it is not held, so a form saved whole waits
    for approval, as the admin API's act takes it, and its approver sees
    only what changes (review of ADR 0180).
    """
    root = Staff(steps, frozen, "root", superuser=True)
    boss = Staff(steps, frozen, "boss", superuser=True)
    stored_name = "call +1 555 0100 123"
    bob = create_user("bob", first_name=stored_name)
    carol = create_user("carol")
    dave = create_user("dave")
    linked = {"is_staff": True, "fields": {"first_name": "www.evil"}}
    whole_form = {
        "is_staff": True,
        "fields": {"first_name": stored_name, "last_name": "Jones"},
    }

    with override_settings(AUTH={**ACTS.settings["AUTH"], "ADMIN_ACTION": False}):
        held_before = pending(root.call("PATCH", f"/users/{dave}", linked))
    with override_settings(AUTH={**REFUSED, "ADMIN_ACTION": False}):
        refused = root.call("PATCH", f"/users/{carol}", linked)
        saved = root.call("PATCH", f"/users/{bob}", whole_form)
    with override_settings(AUTH=REFUSED):
        asked = root.ask(f"/users/{bob}/authorization/patch", whole_form)
        approving = boss.client.post(
            f"{API}/{held_before}/approve/authorization",
            json={"fingerprint": fingerprint_of(boss, held_before)},
            headers=boss.headers,
        )

    problem(refused, 422, "link_not_shown")
    request_id = pending(saved)
    held = {each["id"].hex: each["arguments"] for each in stored_requests()}
    assert held[request_id.replace("-", "")] == {
        "user_id": str(bob),
        "values": {"last_name": "Jones"},
    }
    assert ("Last name", "Jones") in pairs(asked)
    problem(approving, 422, "link_not_shown")
