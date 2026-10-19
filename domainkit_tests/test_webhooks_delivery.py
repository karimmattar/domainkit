"""Webhook deliveries: from a committed event through the outbox to the receiver."""

import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.tables import DONE, messages
from domainkit.contrib.messaging.testing import drain
from domainkit.contrib.webhooks import delivery as delivery_module
from domainkit.contrib.webhooks import expose, queue_deliveries, verify
from domainkit.contrib.webhooks.models import Subscription
from domainkit.contrib.webhooks.tables import (
    CANCELLED,
    FAILED,
    PENDING,
    SUCCEEDED,
    attempts,
    deliveries,
    subscriptions,
)
from domainkit.contrib.webhooks.uow import WebhooksUnitOfWork
from domainkit.db import connections
from domainkit.ddd import DomainEvent
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten, notes
from domainkit_tests.test_messaging import FAST, reported
from domainkit_tests.test_webhooks import FakeResolver, Receiver

pytestmark = pytest.mark.anyio

MESSAGING = "domainkit.contrib.messaging"
WEBHOOKS = "domainkit.contrib.webhooks"
ALPHA = "domainkit_tests.sample_domains.alpha"
SETTINGS: dict[str, Any] = {
    "ALLOWED_PRIVATE_NETWORKS": ["127.0.0.0/8"],
    "TIMEOUT": 2,
    "CONNECT_TIMEOUT": 1,
    "RETRY_BASE_SECONDS": 0.001,
    "RETRY_MAX_SECONDS": 0.001,
    "MAX_ATTEMPTS": 3,
}


class NoteArchived(NoteWritten):
    """A subclass of an exposed event."""


@pytest.fixture
def webhooks(database_url: str) -> Iterator[None]:
    """Messaging and webhooks on an empty database, with http allowed (DEBUG)."""
    reported.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, WEBHOOKS, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MESSAGING={**FAST, "VISIBILITY_TIMEOUT": 60},
        WEBHOOKS=SETTINGS,
        DEBUG=True,
        ERROR_REPORTERS=["domainkit_tests.test_messaging.reporter"],
    ):
        setup()
        yield
    delivery_module._exposed.clear()
    registry.clear()


@pytest.fixture
async def hooks(webhooks: None) -> AsyncIterator[Receiver]:
    """The tables, a receiver, and NoteWritten exposed as ``note.written``."""
    async with connections.engine().begin() as connection:
        for table in (messages, notes, subscriptions, deliveries, attempts):
            await connection.run_sync(table.create)
    expose(NoteWritten, "note.written")
    async with Receiver() as receiver:
        yield receiver
    await connections.close_all()


async def subscribe(url: str, *event_types: str, active: bool = True) -> Subscription:
    """Store a subscription."""
    subscription = Subscription.create(url, "127.0.0.1", event_types, active=active)
    async with WebhooksUnitOfWork() as uow:
        await uow.subscriptions.add(subscription)
        await uow.commit()
    return subscription


async def write_note(event: NoteWritten | None = None) -> NoteWritten:
    """Commit a note that records ``event`` (a NoteWritten by default)."""
    note = Note(id=NoteId(uuid.uuid4()), text="t")
    recorded = event or NoteWritten(note_id=note.id)
    note.record(recorded)
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()
    return recorded


async def rows(table: Any) -> list[Any]:  # noqa: ANN401 - a table.
    """Every row of ``table``."""
    async with connections.engine().connect() as connection:
        return list((await connection.execute(select(table))).all())


async def test_committed_events_reach_their_subscribers(hooks: Receiver) -> None:
    """One signed delivery per subscription that wants the event, logged."""
    named = await subscribe(hooks.url("/named"), "note.written")
    everything = await subscribe(hooks.url("/all"), "*")
    await subscribe(hooks.url("/other"), "invoice.paid")
    await subscribe(hooks.url("/off"), "*", active=False)
    event = await write_note()
    async with NotesUnitOfWork():  # Nothing committed: nothing sent.
        pass

    assert hooks.requests == []
    drained = await drain(delayed=True)

    assert drained.failures == []
    assert sorted(request.target for request in hooks.requests) == ["/all", "/named"]
    secrets = {"/named": named.secret, "/all": everything.secret}
    found = await rows(deliveries)
    assert {row.status for row in found} == {SUCCEEDED}
    ids = {f"msg_{row.id.hex}" for row in found}
    for request in hooks.requests:
        assert verify(secrets[request.target], request.headers, request.body) in ids
        document = json.loads(request.body)
        assert document == {
            "type": "note.written",
            "timestamp": event.occurred_at.isoformat().replace("+00:00", "Z"),
            "data": {
                "event_id": str(event.event_id),
                "occurred_at": document["data"]["occurred_at"],
                "note_id": str(event.note_id),
            },
        }
    log = await rows(attempts)
    assert sorted((entry.number, entry.status_code, entry.error) for entry in log) == [
        (1, 200, None),
        (1, 200, None),
    ]
    assert all(entry.duration_ms >= 0 for entry in log)
    assert {row.attempts for row in found} == {1}
    assert all(row.finished_at is not None for row in found)


async def test_payloads_subclasses_and_retired_exposures(hooks: Receiver) -> None:
    """A payload function picks the data; subclasses go too; unexposed ones not."""
    await subscribe(hooks.url(), "*")
    expose(NoteWritten, "note.written", payload=lambda event: {"id": event.note_id})
    archived = await write_note(NoteArchived(note_id=NoteId(uuid.uuid4())))
    await drain(delayed=True)
    await write_note()
    delivery_module._exposed.clear()  # No longer exposed by the time it runs.
    await drain(delayed=True)

    (request,) = hooks.requests
    assert json.loads(request.body)["data"] == {"id": str(archived.note_id)}
    assert {row.status for row in await rows(messages)} == {DONE}


async def test_a_delivery_is_retried_then_succeeds(hooks: Receiver) -> None:
    """A failed attempt is logged and retried with the same webhook-id."""
    await subscribe(hooks.url(), "note.written")
    hooks.answers.extend([(500, b"down", {}), (503, b"", {})])
    await write_note()

    drained = await drain(delayed=True)

    assert drained.failures == [
        "task domainkit.contrib.webhooks.deliver (retry): DeliveryFailed: HTTP 500.",
        "task domainkit.contrib.webhooks.deliver (retry): DeliveryFailed: HTTP 503.",
    ]
    assert len({request.headers["webhook-id"] for request in hooks.requests}) == 1
    (row,) = await rows(deliveries)
    assert (row.status, row.attempts, row.last_status_code, row.last_error) == (
        SUCCEEDED,
        3,
        200,
        None,
    )
    log = sorted(await rows(attempts), key=lambda entry: entry.number)
    assert [(entry.number, entry.status_code, entry.error) for entry in log] == [
        (1, 500, "HTTP 500."),
        (2, 503, "HTTP 503."),
        (3, 200, None),
    ]


async def test_a_delivery_that_keeps_failing_is_marked_failed(
    hooks: Receiver, caplog: pytest.LogCaptureFixture
) -> None:
    """After MAX_ATTEMPTS the delivery fails; the message is done, not dead."""
    await subscribe(hooks.url("/t/T0K3N"), "note.written")
    hooks.answers.extend([(500, b"", {})] * 3)
    await write_note()

    with caplog.at_level(logging.WARNING, "domainkit.contrib.webhooks.delivery"):
        await drain(delayed=True)

    (row,) = await rows(deliveries)
    assert (row.status, row.attempts, row.last_status_code) == (FAILED, 3, 500)
    assert row.finished_at is not None
    task_rows = [r for r in await rows(messages) if r.kind == "task"]
    assert [r.status for r in task_rows] == [DONE]
    assert caplog.messages[-1] == (
        f"Webhook delivery {row.id} to subscription {row.subscription_id} failed "
        "(attempt 3; giving up): HTTP 500."
    )
    assert "T0K3N" not in caplog.text


async def test_refused_targets_fail_at_once(
    hooks: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL that resolves to a refused address is never retried."""
    resolver = FakeResolver({"rebind.example": ["169.254.169.254"]})
    monkeypatch.setattr("domainkit.contrib.webhooks.guard.lookup", resolver)
    await subscribe("http://rebind.example/hook", "note.written")
    await write_note()

    await drain(delayed=True)

    (row,) = await rows(deliveries)
    assert (row.status, row.attempts) == (FAILED, 1)
    assert row.last_error == (
        "Refused: The webhook URL's host resolves to a cloud metadata address."
    )
    assert resolver.asked == [("rebind.example", 80)]


async def test_inactive_or_deleted_subscriptions_cancel_their_deliveries(
    hooks: Receiver,
) -> None:
    """A delivery whose subscription is off or gone when its turn comes is dropped."""
    off = await subscribe(hooks.url("/off"), "note.written")
    gone = await subscribe(hooks.url("/gone"), "note.written")
    await write_note()
    await Worker(queues=["default"]).run_once()  # The fan-out; deliveries wait.
    async with WebhooksUnitOfWork() as uow:
        (await uow.subscriptions.get(off.id)).change(active=False)
        await uow.subscriptions.remove(await uow.subscriptions.get(gone.id))
        await uow.commit()
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).values(available_at=datetime.now(UTC))
        )

    await drain(delayed=True)

    assert hooks.requests == []
    assert {row.status for row in await rows(deliveries)} == {CANCELLED}
    assert {row.last_error for row in await rows(deliveries)} == {
        "The subscription is gone or inactive."
    }


async def test_events_are_queued_once_per_subscriber(hooks: Receiver) -> None:
    """Queueing the same event again (a retried fan-out) adds nothing."""
    await subscribe(hooks.url(), "note.written")
    now = datetime.now(UTC)

    first = await queue_deliveries("note.written", {"a": 1}, key="e1", occurred_at=now)
    again = await queue_deliveries("note.written", {"a": 1}, key="e1", occurred_at=now)
    unwanted = await queue_deliveries("other", {}, key="e2", occurred_at=now)

    assert (first, again, unwanted) == (1, 1, 0)
    assert len(await rows(deliveries)) == 1
    assert len(await rows(messages)) == 1


async def test_oversized_events_are_reported_and_dropped(
    hooks: Receiver, caplog: pytest.LogCaptureFixture
) -> None:
    """A body over MAX_PAYLOAD_BYTES cannot be sent; retrying cannot help."""
    await subscribe(hooks.url(), "*")
    expose(NoteWritten, "note.written", payload=lambda event: {"x": "y" * 300_000})
    await write_note()

    with caplog.at_level(logging.ERROR, "domainkit.contrib.webhooks.delivery"):
        await drain(delayed=True)

    assert await rows(deliveries) == []
    assert {row.status for row in await rows(messages)} == {DONE}
    ((text, context),) = reported
    assert text.startswith("The note.written webhook body has 300")
    assert context == {"event_type": "note.written"}
    assert caplog.messages[0].startswith("Dropped a webhook event: ")


async def test_finished_deliveries_can_be_replayed(hooks: Receiver) -> None:
    """A replay sends again under the same id; pending ones stay as they are."""
    await subscribe(hooks.url(), "note.written")
    hooks.answers.extend([(500, b"", {})] * 3)
    await write_note()
    await drain(delayed=True)
    (failed,) = await rows(deliveries)

    assert await delivery_module.failed_deliveries(10) == [failed.id]
    assert await delivery_module.failed_deliveries(10, uuid.uuid4()) == []
    assert await delivery_module.replay([failed.id, uuid.uuid4()]) == [failed.id]
    assert await delivery_module.replay([failed.id]) == []  # Pending again.
    assert await delivery_module.replay([]) == []
    await drain(delayed=True)

    (row,) = await rows(deliveries)
    assert (row.status, row.attempts) == (SUCCEEDED, 4)
    assert len({request.headers["webhook-id"] for request in hooks.requests}) == 1
    assert await delivery_module.failed_deliveries(10) == []


async def test_deliveries_that_are_not_pending_are_left_alone(
    hooks: Receiver,
) -> None:
    """A duplicate run of a finished delivery, or of a missing one, does nothing."""
    await subscribe(hooks.url(), "note.written")
    await write_note()
    await drain(delayed=True)
    (row,) = await rows(deliveries)

    await delivery_module.deliver(row.id)
    await delivery_module.deliver(uuid.uuid4())

    assert len(hooks.requests) == 1


async def test_a_direct_call_makes_one_final_attempt(hooks: Receiver) -> None:
    """Outside the worker there is no retry: a failure is final."""
    await subscribe(hooks.url(), "note.written")
    hooks.answers.append((500, b"", {}))
    now = datetime.now(UTC)
    await queue_deliveries("note.written", {}, key="k", occurred_at=now)
    (row,) = await rows(deliveries)

    await delivery_module.deliver(row.id)

    (done,) = await rows(deliveries)
    assert (done.status, done.attempts) == (FAILED, 1)


async def test_rotated_secrets_sign_side_by_side(hooks: Receiver) -> None:
    """During the overlap, deliveries carry both signatures."""
    subscription = await subscribe(hooks.url(), "note.written")
    old = subscription.secret
    async with WebhooksUnitOfWork() as uow:
        stored = await uow.subscriptions.get(subscription.id)
        new = stored.rotate_secret(timedelta(hours=1))
        await uow.commit()
    await write_note()

    await drain(delayed=True)

    (request,) = hooks.requests
    assert len(request.headers["webhook-signature"].split()) == 2
    for secret in (old, new):
        verify(secret, request.headers, request.body)


async def test_old_finished_deliveries_are_purged(
    hooks: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finished deliveries and their attempts go after the retention; others stay."""
    monkeypatch.setattr(delivery_module, "PURGE_BATCH", 2)
    await subscribe(hooks.url(), "*")
    for key in "abcde":
        await queue_deliveries("x", {}, key=key, occurred_at=datetime.now(UTC))
    await drain(delayed=True)
    await queue_deliveries("x", {}, key="pending", occurred_at=datetime.now(UTC))
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(deliveries)
            .where(deliveries.c.status == SUCCEEDED)
            .values(finished_at=datetime.now(UTC) - timedelta(days=60))
        )

    assert await delivery_module.purge(timedelta(days=30)) == 5
    assert [row.status for row in await rows(deliveries)] == [PENDING]
    assert await rows(attempts) == []
    assert await delivery_module.purge(timedelta(days=30)) == 0


async def test_the_purge_runs_hourly(hooks: Receiver) -> None:
    """The periodic task deletes what is older than RETENTION_SECONDS."""
    from domainkit.contrib.messaging import registry as tasks

    spec = tasks.get_task("domainkit.contrib.webhooks.purge")
    assert spec is not None
    assert spec.every == timedelta(hours=1)
    await spec.function()


def test_webhooks_need_messaging(database_url: str) -> None:
    """Without messaging there is nothing to deliver with."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[WEBHOOKS, ALPHA],
            DATABASES={"default": {"URL": database_url}},
        ),
        pytest.raises(
            ImproperlyConfigured, match=r"needs 'domainkit\.contrib\.messaging'"
        ),
    ):
        setup()
    registry.clear()


DEPLOYED: dict[str, Any] = {
    "ALLOWED_HOSTS": ["api.example.com"],
    "DATABASES": {
        "default": {"URL": "postgresql+asyncpg://db/app", "SSLMODE": "verify-full"}
    },
}


@pytest.mark.parametrize(
    ("networks", "refused"),
    [
        (["10.0.0.0/8", "fd00::/8"], None),
        (["127.0.0.0/8"], "127.0.0.0/8"),
        (["10.0.0.0/8", "0.0.0.0/0"], "0.0.0.0/0"),
        (["169.254.0.0/16"], "169.254.0.0/16"),
        (["::/0"], "::/0"),
        (["fe80::/64"], "fe80::/64"),
    ],
)
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_refuse_local_networks(
    environment: str, networks: list[str], refused: str | None
) -> None:
    """Staging and production may allow private networks, not local ones."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, WEBHOOKS],
        ALPHA=UNSET,
        ENVIRONMENT=environment,
        WEBHOOKS={"ALLOWED_PRIVATE_NETWORKS": networks},
        **DEPLOYED,
    ):
        if refused is None:
            setup()
        else:
            with pytest.raises(ImproperlyConfigured) as error:
                setup()
            assert str(error.value) == (
                "WEBHOOKS.ALLOWED_PRIVATE_NETWORKS may not reach loopback or "
                f"link-local addresses in {environment}: {refused}."
            )
    registry.clear()


def test_exposed_names_are_checked() -> None:
    """Event type names follow the subscription rules; '*' is not one."""
    for name in ("*", "bad name", ""):
        with pytest.raises(ImproperlyConfigured, match="Webhook event types are"):
            expose(NoteWritten, name)
    assert delivery_module._exposed == {}


def test_only_postgresql_and_sqlite_keep_deliveries() -> None:
    """Other dialects are refused."""
    with pytest.raises(ImproperlyConfigured, match="PostgreSQL or SQLite, not mysql"):
        delivery_module._insert_new("mysql", [])


def test_exposures_are_found_through_base_classes() -> None:
    """A subclass inherits its base class's exposure."""
    delivery_module._exposed[NoteWritten] = delivery_module.Exposure("n", None)
    try:
        assert delivery_module.exposure_for(NoteArchived) is not None
        assert delivery_module.exposure_for(DomainEvent) is None
    finally:
        delivery_module._exposed.clear()
