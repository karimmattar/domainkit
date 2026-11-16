"""Each event names who caused it: ADR 0147 (user report A11).

The report: "Events carry no actor: a handler cannot tell a change staff
made from the user's own." Every event now carries ``actor``, filled in
from the request, job, or command it was made in: the user, staff
impersonating one, an operator, or the system. The stores that send
events out (webhooks, live events) and the audit trail, which has an
actor column of its own, leave it out of what they wrote before.
"""

import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from domainkit.context import (
    SYSTEM_ACTOR,
    RequestContext,
    bind_request,
    current_actor,
)
from domainkit.contrib.audit.recorder import event_rows
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.messaging import Worker, enqueue, task
from domainkit.contrib.messaging.outbox import context_headers
from domainkit.contrib.messaging.registry import dump_event, load_event
from domainkit.contrib.webhooks.delivery import Exposure, event_data
from domainkit.ddd import Actor, DomainEvent, event_bus
from domainkit.ddd.events import inherits_actor
from domainkit.http.live_events import payload
from domainkit.management.operators import Operator, acting_as
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    messaging,
    tables,
)

STAFF = "6a3c1d52-9b0e-4c55-8a0f-1f2e3d4c5b6a"
ANN = "0c9e8f7a-2b1d-4e3f-9a8b-7c6d5e4f3a2b"


class Shipped(DomainEvent):
    """An order left the warehouse."""

    order_id: uuid.UUID


class Signed(DomainEvent):
    """An event from before events had an actor, with a field of that name."""

    actor: str  # type: ignore[assignment]  # A project's own, kept as it was.


def context(actor: str | None, impersonating: str | None = None) -> RequestContext:
    """A request of ``actor``'s, acting as ``impersonating`` if given."""
    return RequestContext(
        "req-1", None, "POST", "/orders", actor=actor, impersonating=impersonating
    )


def shipped() -> Shipped:
    """A new ``Shipped``."""
    return Shipped(order_id=uuid.UUID(int=1))


def test_an_event_names_the_actor_of_the_request_it_was_made_in() -> None:
    """The user, staff acting as a user, nobody for anonymous callers or none."""
    with bind_request(context(ANN)):
        assert shipped().actor == Actor(ANN)
        assert current_actor() == Actor(ANN)
    with bind_request(context(STAFF, impersonating=ANN)):
        assert shipped().actor == Actor(STAFF, impersonating=ANN)
    with bind_request(context(None)):
        assert shipped().actor is None
    assert shipped().actor is None
    assert current_actor() is None


def test_an_actor_given_to_the_event_wins() -> None:
    """A domain that knows better names the actor itself."""
    with bind_request(context(ANN)):
        made = Shipped(order_id=uuid.UUID(int=1), actor=Actor(SYSTEM_ACTOR))

    assert made.actor == Actor("system")
    assert dataclasses.replace(made, order_id=uuid.UUID(int=2)).actor == made.actor


def test_an_actor_tells_the_user_from_staff_an_operator_and_the_system() -> None:
    """``is_self`` holds for the subject's own acts only; ``is_system`` for none."""
    subject = uuid.UUID(ANN)

    assert Actor(ANN).is_self(subject)
    assert Actor(ANN).is_self(ANN)
    assert not Actor(STAFF).is_self(subject)  # Staff acting on them.
    assert not Actor(STAFF, impersonating=ANN).is_self(subject)
    assert not Actor(ANN, impersonating=STAFF).is_self(subject)
    assert not Actor("operator:alice").is_self(subject)
    assert Actor(SYSTEM_ACTOR).is_system
    assert not Actor(ANN).is_system
    assert not Actor(SYSTEM_ACTOR).is_self(subject)


def test_a_command_acts_as_its_operator() -> None:
    """``operator:<name>``: who ran the command, as the audit trail says."""
    with acting_as(Operator("alice", "option"), "privacy"):
        assert shipped().actor == Actor("operator:alice")


def test_the_actor_travels_with_the_event_and_old_messages_take_the_jobs() -> None:
    """The outbox keeps it; a message queued before it existed has the job's."""
    with bind_request(context(STAFF, impersonating=ANN)):
        made = shipped()
    dumped = dump_event(made)

    assert dumped["actor"] == {"id": STAFF, "impersonating": ANN}
    assert load_event(Shipped, dumped) == made
    queued_before = {key: value for key, value in dumped.items() if key != "actor"}
    with bind_request(context("bob")):
        assert load_event(Shipped, queued_before).actor == Actor("bob")
    assert load_event(Shipped, {**dumped, "actor": None}).actor is None


def test_the_worker_hears_whom_staff_act_as() -> None:
    """The outbox carries the impersonated user's id next to the actor's."""
    with bind_request(context(STAFF, impersonating=ANN)):
        headers = context_headers()
    with bind_request(context(ANN)):
        plain = context_headers()

    assert (headers["actor"], headers["impersonating"]) == (STAFF, ANN)
    assert "impersonating" not in plain


def test_what_events_held_before_is_unchanged_where_they_leave() -> None:
    """Webhook bodies, live events, and audit details hold no ``actor``.

    The audit row names the actor in a column of its own; a webhook
    receiver or a browser is not told which staff member acted. An event
    with an ``actor`` field of its own keeps it everywhere.
    """
    with bind_request(context(STAFF, impersonating=ANN)):
        made = shipped()
        signed = Signed(actor="the clerk")

    assert inherits_actor(Shipped)
    assert not inherits_actor(Signed)
    assert "actor" not in payload(made)
    assert payload(signed)["actor"] == "the clerk"
    assert "actor" not in event_data(Exposure("orders.shipped", None), made)
    assert event_data(Exposure("signed", None), signed)["actor"] == "the clerk"
    chosen = Exposure("orders.shipped", lambda event: {"by": event.actor.id})
    assert event_data(chosen, made) == {"by": STAFF}
    rows = event_rows([made, signed], AuditSettings())
    assert "actor" not in rows[0].row["details"]
    assert rows[1].row["details"]["actor"] == "the clerk"


made_in_the_worker: list[Actor | None] = []


@task(name="tests.event_actor.ships")
async def ships() -> None:
    """Make an event in the worker."""
    made_in_the_worker.append(shipped().actor)


async def keep_the_actor(event: NoteWritten) -> None:
    """A durable handler: keep the actor the event names."""
    made_in_the_worker.append(event.actor)


@pytest.fixture
def worker_runs() -> Iterator[list[Actor | None]]:
    """What the worker's tasks and handlers saw, cleared."""
    made_in_the_worker.clear()
    yield made_in_the_worker
    made_in_the_worker.clear()


@pytest.mark.anyio
async def test_a_durable_handler_reads_the_actor_from_the_event(
    tables: None,  # noqa: F811 - the fixture.
    worker_runs: list[Any],
) -> None:
    """The handler runs later, in the worker; the event still names who acted."""
    event_bus.subscribe(NoteWritten, keep_the_actor, durable=True)
    with bind_request(context(STAFF, impersonating=ANN)):
        note = Note(id=NoteId(uuid.uuid4()), text="t")
        note.record(NoteWritten(note_id=note.id))
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(note)
            await uow.commit()

    await Worker().run_once()

    assert worker_runs == [Actor(STAFF, impersonating=ANN)]


@pytest.mark.anyio
async def test_work_in_the_worker_acts_as_who_queued_it_or_the_system(
    tables: None,  # noqa: F811 - the fixture.
    worker_runs: list[Any],
) -> None:
    """A job a request queued acts as its actor; one nobody queued, as the system."""
    with bind_request(context(STAFF, impersonating=ANN)):
        await enqueue(ships)
    await enqueue(ships)

    await Worker().run_once()

    assert sorted(worker_runs, key=str) == [
        Actor(STAFF, impersonating=ANN),
        Actor(SYSTEM_ACTOR),
    ]
    assert [actor.is_system for actor in worker_runs].count(True) == 1
