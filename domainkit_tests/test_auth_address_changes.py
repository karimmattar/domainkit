"""A changed address names the one it replaced; events say who acted (ADR 0147).

The report (A11): "``UserUpdated`` carries field names only: a handler
cannot tell a change staff made from the user's own, nor notify the old
address." OWASP and ASVS 5.0 (V6.3.7) ask to tell the user at the old
address. ``EmailChanged`` and ``MobileNumberChanged`` carry it, marked
``Personal``, and whether the user had proved it; ``UserUpdated`` stays
names only. Every event names its actor.
"""

from collections.abc import Iterator
from uuid import UUID

import pytest

from domainkit.contrib.audit.recorder import event_rows
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.auth import PasswordHash, User
from domainkit.contrib.auth.events import UserLoggedOut, UserUpdated
from domainkit.contrib.auth.exceptions import IdentifierRequired
from domainkit.contrib.auth.integration import EmailChanged, MobileNumberChanged
from domainkit.contrib.messaging.registry import dump_event
from domainkit.contrib.messaging.testing import CapturedEvents
from domainkit.ddd import Actor, DomainEvent, Personal, event_bus
from domainkit.ddd.personal import personal_fields, subjects_in
from domainkit.http.live_events import payload
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import audited, bearer, create_user
from domainkit_tests.test_auth_admin import ADMIN, staff, token_for
from domainkit_tests.test_auth_impersonation import ON, impersonate

OLD = "ann@example.com"
NUMBER = "+33612345678"


def ann(**fields: object) -> User:
    """A user with an address and a number, their creation event taken."""
    user = User.create(
        password=PasswordHash.unusable(),
        username="ann",
        **{"email": OLD, "mobile_number": NUMBER, **fields},
    )
    user.pull_events()
    return user


def kinds(events: list[DomainEvent]) -> list[str]:
    """The class names of ``events``."""
    return [type(event).__name__ for event in events]


def test_a_new_address_names_the_one_it_replaced() -> None:
    """``EmailChanged`` follows ``UserUpdated``, which still names fields only."""
    user = ann(email_verified=True)

    user.update(email="ann@new.example", first_name="Ann")

    updated, changed = user.pull_events()
    assert isinstance(updated, UserUpdated)
    assert updated.fields == ("email", "first_name")
    assert isinstance(changed, EmailChanged)
    assert (changed.user_id, changed.previous, changed.previous_verified) == (
        user.id,
        OLD,
        True,
    )
    user.update(email="ann@NEW.example")  # The same address, normalized.
    user.update(first_name="Annie")
    assert kinds(user.pull_events()) == ["UserUpdated"]


def test_a_new_number_names_the_one_it_replaced_or_none() -> None:
    """A first number replaces none; a removed one is named too."""
    user = ann()
    lone = ann(mobile_number=None)

    user.update(mobile_number="+33 6 98 76 54 32")
    lone.update(mobile_number=NUMBER)
    user.update(mobile_number=None)

    first, removed = user.pull_events()[1::2]
    assert isinstance(first, MobileNumberChanged)
    assert (first.previous, first.previous_verified) == (NUMBER, False)
    assert isinstance(removed, MobileNumberChanged)
    assert removed.previous == "+33698765432"
    (added,) = [e for e in lone.pull_events() if isinstance(e, MobileNumberChanged)]
    assert (added.previous, added.previous_verified) == (None, False)


def test_an_address_added_or_removed_names_none_or_the_old_one() -> None:
    """An account with no address gets one; one that drops it names the old one."""
    user = ann(email="")

    user.update(email=OLD)
    user.update(email="")

    added, removed = [e for e in user.pull_events() if isinstance(e, EmailChanged)]
    assert (added.previous, removed.previous) == (None, OLD)


def test_a_refused_change_erasure_and_release_name_no_old_address() -> None:
    """Only a change the user keeps is told; erasure must not copy the data again."""
    only_email = User.create(password=PasswordHash.unusable(), email=OLD)
    only_email.pull_events()
    with pytest.raises(IdentifierRequired):
        only_email.update(email="")
    erased = ann()
    released = ann()

    erased.erase_personal_data(frozenset({"email", "mobile_number"}))
    released.release_email()
    released.release_mobile_number()

    assert "EmailChanged" not in kinds(only_email.pull_events())
    for user in (erased, released):
        assert not {"EmailChanged", "MobileNumberChanged"} & set(
            kinds(user.pull_events())
        )


def test_the_old_address_is_personal_data_wherever_it_is_kept() -> None:
    """A pseudonym in the audit trail; found by erasure in the outbox; never live."""
    user = ann()
    user.update(email="ann@new.example")
    changed = user.pull_events()[1]

    assert personal_fields(EmailChanged) == {"previous": Personal(of="user_id")}
    assert personal_fields(MobileNumberChanged) == {"previous": Personal(of="user_id")}
    (draft,) = event_rows([changed], AuditSettings())
    assert "previous" not in draft.row["details"]
    assert [(item.name, item.subject) for item in draft.personal] == [
        ("previous", str(user.id))
    ]
    assert subjects_in(EmailChanged, dump_event(changed)) == {str(user.id)}
    assert "previous" not in payload(changed)


@pytest.fixture
def captured() -> Iterator[CapturedEvents]:
    """The events the application publishes."""
    events = CapturedEvents()
    event_bus.subscribe(DomainEvent, events.record)
    yield events
    event_bus.unsubscribe(DomainEvent, events.record)


def test_a_change_staff_made_is_told_from_the_users_own(
    api: Api, captured: CapturedEvents
) -> None:
    """The admin API's change names the clerk; the handler can tell them apart."""
    client = api()
    clerk = staff("clerk", "auth.change_user", "auth.view_user")
    clerk_id = client.get("/auth/me", headers=clerk).json()["id"]
    user_id = create_user("ann", email=OLD, proved=True)

    response = client.patch(
        f"{ADMIN}/users/{user_id}",
        json={"fields": {"email": "ann@new.example"}},
        headers=clerk,
    )

    assert response.status_code == 200, response.json()
    changed = captured.assert_published(EmailChanged, user_id=user_id)
    assert (changed.previous, changed.previous_verified) == (OLD, True)
    assert changed.actor == Actor(clerk_id)
    assert not changed.actor.is_self(changed.user_id)
    updated = captured.assert_published(UserUpdated, user_id=user_id)
    assert updated.actor == changed.actor
    (row,) = audited("auth.email_changed")
    assert row["actor"] == clerk_id
    assert row["details"]["previous"] != OLD  # A pseudonym.
    assert row["details"]["previous_verified"] is True
    assert "actor" not in row["details"]


def test_an_impersonations_events_name_the_staff_member_and_the_user(
    api: Api, captured: CapturedEvents
) -> None:
    """Staff acting as ann are not ann: ``impersonating`` says whom they act as."""
    client = api(**ON)
    boss = staff("boss", is_superuser=True)
    boss_id = client.get("/auth/me", headers=boss).json()["id"]
    ann_id = create_user("ann")
    as_ann = bearer(impersonate(client, ann_id, boss).json()["access_token"])

    client.post("/auth/logout", headers=as_ann)
    client.post("/auth/logout", headers=token_for("ann"))

    by_staff, by_ann = captured.of_type(UserLoggedOut)
    assert by_staff.actor == Actor(boss_id, impersonating=str(ann_id))
    assert not by_staff.actor.is_self(ann_id)
    assert by_ann.actor == Actor(str(ann_id))
    assert by_ann.actor.is_self(UUID(str(ann_id)))
