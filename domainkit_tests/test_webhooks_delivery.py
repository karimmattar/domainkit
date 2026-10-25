"""Webhook deliveries: from a committed event through the outbox to the receiver."""

import asyncio
import json
import logging
import socket
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from domainkit import clock, lifecycle, setup
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.tables import DONE, messages
from domainkit.contrib.messaging.testing import drain
from domainkit.contrib.webhooks import delivery as delivery_module
from domainkit.contrib.webhooks import expose, queue_deliveries, verify
from domainkit.contrib.webhooks.delivery import KEY_LOST
from domainkit.contrib.webhooks.models import Subscription, SubscriptionId, secret_box
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
from domainkit.http import guard
from domainkit.lifecycle import Phase
from domainkit.reporting import flush_reports
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


OLD_KEY = "old-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJ"
NEW_KEY = "new-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJ"


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


async def subscribe(
    url: str, *event_types: str, active: bool = True
) -> tuple[Subscription, str]:
    """Store a subscription; return it and its secret."""
    subscription, secret = Subscription.create(
        url, "127.0.0.1", event_types, active=active
    )
    async with WebhooksUnitOfWork() as uow:
        await uow.subscriptions.add(subscription)
        await uow.commit()
    return subscription, secret


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
    _, named = await subscribe(hooks.url("/named"), "note.written")
    _, everything = await subscribe(hooks.url("/all"), "*")
    await subscribe(hooks.url("/other"), "invoice.paid")
    await subscribe(hooks.url("/off"), "*", active=False)
    event = await write_note()
    async with NotesUnitOfWork():  # Nothing committed: nothing sent.
        pass

    assert hooks.requests == []
    drained = await drain(delayed=True)

    assert drained.failures == []
    assert sorted(request.target for request in hooks.requests) == ["/all", "/named"]
    secrets = {"/named": named, "/all": everything}
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
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "domainkit.contrib.webhooks.delivery"
    ] == [
        f"Webhook delivery {row.id} to subscription {row.subscription_id} failed "
        f"(attempt {number}; {verdict}): HTTP 500."
        for number, verdict in ((1, "retrying"), (2, "retrying"), (3, "giving up"))
    ]
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
    off, _ = await subscribe(hooks.url("/off"), "note.written")
    gone, _ = await subscribe(hooks.url("/gone"), "note.written")
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
    flush_reports()  # Reporters run on their own thread.
    ((text, context),) = reported
    assert text.startswith("The note.written webhook body has 300")
    assert context == {"event_type": "note.written"}
    assert caplog.messages[0].startswith("Dropped a webhook event: ")


async def test_bodies_may_be_exactly_the_largest_size(hooks: Receiver) -> None:
    """MAX_PAYLOAD_BYTES is the largest body allowed, not the first refused."""
    await subscribe(hooks.url(), "*")
    now = datetime.now(UTC)
    size = len(delivery_module.body_of("x", {"d": ""}, now))
    limit = 1024
    exact = {"d": "y" * (limit - size)}
    assert len(delivery_module.body_of("x", exact, now)) == limit

    with override_settings(WEBHOOKS={**SETTINGS, "MAX_PAYLOAD_BYTES": limit}):
        assert await queue_deliveries("x", exact, key="fits", occurred_at=now) == 1
        with pytest.raises(delivery_module.PayloadTooLarge, match="has 1025 bytes"):
            await queue_deliveries(
                "x", {"d": "y" * (limit - size + 1)}, key="over", occurred_at=now
            )


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


async def test_a_failed_attempt_with_attempts_left_stays_pending(
    hooks: Receiver,
) -> None:
    """It raises for the worker to retry, and the delivery is not finished."""
    await subscribe(hooks.url(), "note.written")
    hooks.answers.append((500, b"", {}))
    await queue_deliveries("note.written", {}, key="k", occurred_at=datetime.now(UTC))
    (row,) = await rows(deliveries)
    token = worker_module._attempt.set(worker_module.Attempt(uuid.uuid4(), 1, 3))
    try:
        with pytest.raises(delivery_module.DeliveryFailed, match=r"^HTTP 500\.$"):
            await delivery_module.deliver(row.id)
    finally:
        worker_module._attempt.reset(token)

    (pending,) = await rows(deliveries)
    assert (pending.status, pending.attempts, pending.finished_at) == (PENDING, 1, None)
    assert pending.last_attempt_at is not None


async def test_a_fan_out_that_runs_twice_queues_once(hooks: Receiver) -> None:
    """A durable handler may run twice; the event's id keeps deliveries single."""
    await subscribe(hooks.url(), "note.written")
    event = NoteWritten(note_id=NoteId(uuid.uuid4()))

    await delivery_module.fan_out(event)
    await delivery_module.fan_out(event)
    await delivery_module.fan_out(NoteWritten(note_id=event.note_id))  # Another.

    assert len(await rows(deliveries)) == 2


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


# The real resolver, for names the fakes below do not own: the test database's
# host among them (``localhost`` in CI, which asyncpg resolves through
# ``loop.getaddrinfo`` with every argument given by position).
REAL_GETADDRINFO = socket.getaddrinfo


class HungDns:
    """``getaddrinfo``: ``*.hang.test`` waits, then fails; other ``.test`` are local."""

    def __init__(self, hung: int) -> None:
        """Let ``all_hung`` be set once ``hung`` lookups wait."""
        self.hung = hung
        self.waiting = 0
        self.all_hung = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, host: str, port: int, *args: Any, **options: Any) -> list[Any]:  # noqa: ANN401
        """Wait for ``release`` and fail, for a hung host; else answer at once."""
        if not host.endswith(".test"):
            return REAL_GETADDRINFO(host, port, *args, **options)
        if host.endswith(".hang.test"):
            with self._lock:
                self.waiting += 1
                if self.waiting == self.hung:
                    self.all_hung.set()
            assert self.release.wait(30)
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


async def test_one_subscribers_hung_hosts_leave_lookups_for_the_others(
    hooks: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four subscriptions whose DNS never answers; another's delivery still goes.

    The worker runs every delivery of an event as the event's actor (here an
    anonymous request's, ``ip:unknown``): the lookups are each
    subscription's own, so the hung ones fill no shared quarter.
    """
    dns = HungDns(hung=4)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(guard, "resolver", guard.GuardResolver())
    saved = dict(lifecycle._registered[Phase.RESOURCES])
    hung = [
        (await subscribe(f"http://s{i}.hang.test:{hooks.port}/hook", "note.written"))[0]
        for i in range(4)
    ]
    victim, _ = await subscribe(hooks.url(host="victim.test"), "note.written")
    await queue_deliveries("note.written", {}, key="k", occurred_at=datetime.now(UTC))
    by_subscription = {row.subscription_id: row.id for row in await rows(deliveries)}
    queued_by = RequestContext("req-1", None, "TASK", "webhooks.deliver")
    try:
        with bind_request(queued_by):
            waiting = [
                asyncio.create_task(delivery_module.deliver(by_subscription[s.id]))
                for s in hung
            ]
            assert await asyncio.to_thread(dns.all_hung.wait, 30)
            owners = dict(guard.resolver._owners)
            await delivery_module.deliver(by_subscription[victim.id])
        dns.release.set()
        await asyncio.gather(*waiting)
    finally:
        dns.release.set()
        await guard.resolver.close()
        lifecycle._registered[Phase.RESOURCES].clear()
        lifecycle._registered[Phase.RESOURCES].update(saved)

    statuses = {row.subscription_id: row.status for row in await rows(deliveries)}
    assert statuses[victim.id] == SUCCEEDED
    assert owners == {f"webhook:{s.id}": 1 for s in hung}
    assert [statuses[s.id] for s in hung] == [FAILED] * 4
    assert [request.target for request in hooks.requests] == ["/hook"]


class SlowDns:
    """``*.slow.test`` waits, then fails or answers; other ``.test`` names are local.

    The test moves the clock past ``HUNG_LOOKUP_SECONDS`` before it lets
    them go, so each took that long.
    """

    def __init__(self, waiting_for: int, *, succeed: bool) -> None:
        """Set ``all_waiting`` once ``waiting_for`` lookups wait."""
        self.waiting_for = waiting_for
        self.succeed = succeed
        self.slow_lookups = 0
        self.all_waiting = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, host: str, port: int, *args: Any, **options: Any) -> list[Any]:  # noqa: ANN401
        """Wait for ``release`` on a slow host; answer 127.0.0.1, or fail."""
        if not host.endswith(".test"):
            return REAL_GETADDRINFO(host, port, *args, **options)
        if host.endswith(".slow.test"):
            with self._lock:
                self.slow_lookups += 1
                if self.slow_lookups % self.waiting_for == 0:
                    self.all_waiting.set()
            assert self.release.wait(30)
            if not self.succeed:
                raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


@pytest.mark.parametrize("succeed", [False, True], ids=["slow-fail", "slow-success"])
async def test_hosts_one_owner_slows_down_hold_the_lookups_one_round_only(
    hooks: Receiver, monkeypatch: pytest.MonkeyPatch, succeed: bool
) -> None:
    """Eight subscriptions' slow hosts take every thread once, not every time.

    Two deliveries each hold all 16 lookup threads, and another
    subscriber's delivery is refused (until 0.10 gives subscriptions an
    owner to share by). Their lookups took over 5 seconds: a failure backs
    its host off, and a success is kept (SEC-r1-5), so the next event's
    deliveries take no thread and the other subscriber's goes through.
    """
    now = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    dns = SlowDns(16, succeed=succeed)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(guard, "resolver", guard.GuardResolver())
    saved = dict(lifecycle._registered[Phase.RESOURCES])
    slow = {
        (await subscribe(f"http://s{i}.slow.test:{hooks.port}/hook", "note.written"))[
            0
        ].id
        for i in range(8)
    }
    victim, _ = await subscribe(hooks.url(host="victim.test"), "note.written")
    queued_by = RequestContext("req-1", None, "TASK", "webhooks.deliver")
    outcomes: list[tuple[str, int]] = []
    try:
        for event in range(2):
            for copy in range(2):
                await queue_deliveries(
                    "note.written",
                    {},
                    key=f"event{event}-{copy}",
                    occurred_at=datetime.now(UTC),
                )
            pending = [row for row in await rows(deliveries) if row.status == PENDING]
            lookups_before = dns.slow_lookups
            with bind_request(queued_by):
                theirs = [
                    asyncio.create_task(delivery_module.deliver(row.id))
                    for row in pending
                    if row.subscription_id in slow
                ]
                held: asyncio.Future[Any] = asyncio.ensure_future(
                    asyncio.to_thread(dns.all_waiting.wait, 30)
                )
                all_theirs: asyncio.Future[Any] = asyncio.gather(*theirs)
                await asyncio.wait(
                    [held, all_theirs], return_when=asyncio.FIRST_COMPLETED
                )
                mine = [row.id for row in pending if row.subscription_id == victim.id]
                for delivery_id in mine:
                    await delivery_module.deliver(delivery_id)
            now[0] += guard.HUNG_LOOKUP_SECONDS
            dns.release.set()
            dns.all_waiting.set()  # Ends a wait that nothing else would.
            await asyncio.gather(held, all_theirs)
            dns.all_waiting.clear()
            dns.release.clear()
            statuses = {row.id: row.status for row in await rows(deliveries)}
            status = "/".join(sorted({statuses[delivery_id] for delivery_id in mine}))
            outcomes.append((status, dns.slow_lookups - lookups_before))
    finally:
        dns.release.set()
        dns.all_waiting.set()
        await guard.resolver.close()
        lifecycle._registered[Phase.RESOURCES].clear()
        lifecycle._registered[Phase.RESOURCES].update(saved)

    assert outcomes == [(FAILED, 16), (SUCCEEDED, 0)]


async def test_rotated_secrets_sign_side_by_side(hooks: Receiver) -> None:
    """During the overlap, deliveries carry both signatures."""
    subscription, old = await subscribe(hooks.url(), "note.written")
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


async def stored_secrets(subscription_id: uuid.UUID) -> tuple[bytes, bytes | None]:
    """The secret and previous secret a subscription's row holds."""
    async with connections.engine().connect() as connection:
        row = (
            await connection.execute(
                select(subscriptions.c.secret, subscriptions.c.previous_secret).where(
                    subscriptions.c.id == subscription_id
                )
            )
        ).one()
    return row.secret, row.previous_secret


async def rotate(subscription_id: SubscriptionId) -> str:
    """Rotate a subscription's secret, with an hour of overlap; return the new one."""
    async with WebhooksUnitOfWork() as uow:
        stored = await uow.subscriptions.get(subscription_id)
        secret = stored.rotate_secret(timedelta(hours=1))
        await uow.commit()
    return secret


async def test_secrets_are_stored_encrypted(hooks: Receiver) -> None:
    """The row holds ciphertext bound to it; a delivery decrypts it to sign."""
    subscription, secret = await subscribe(hooks.url(), "note.written")
    sealed, previous = await stored_secrets(subscription.id)
    await write_note()

    await drain(delayed=True)

    (request,) = hooks.requests
    verify(secret, request.headers, request.body)
    assert previous is None
    assert secret.encode() not in sealed
    assert secret_box().decrypt(sealed, associated=subscription.id.bytes) == (
        secret.encode(),
        True,
    )
    assert await stored_secrets(subscription.id) == (sealed, None)  # Left as it is.


async def test_secrets_no_key_decrypts_fail_their_deliveries(
    hooks: Receiver, caplog: pytest.LogCaptureFixture
) -> None:
    """A tampered secret, or one copied from another row: nothing is sent."""
    tampered, _ = await subscribe(hooks.url("/tampered"), "note.written")
    copied, _ = await subscribe(hooks.url("/copied"), "note.written")
    rotating, _ = await subscribe(hooks.url("/rotating"), "note.written")
    donor, donor_secret = await subscribe(hooks.url("/donor"), "note.written")
    await rotate(rotating.id)
    sealed, _ = await stored_secrets(tampered.id)
    donor_sealed, _ = await stored_secrets(donor.id)
    _, previous = await stored_secrets(rotating.id)
    assert previous is not None
    async with connections.engine().begin() as connection:
        for subscription_id, column, value in (
            (tampered.id, "secret", sealed[:-1] + bytes([sealed[-1] ^ 1])),
            (copied.id, "secret", donor_sealed),
            (rotating.id, "previous_secret", previous[:-1] + b"\0"),
        ):
            await connection.execute(
                update(subscriptions)
                .where(subscriptions.c.id == subscription_id)
                .values({column: value})
            )
    await write_note()

    with caplog.at_level(logging.INFO, "domainkit.contrib.webhooks.delivery"):
        drained = await drain(delayed=True)

    assert drained.failures == []
    (request,) = hooks.requests
    assert request.target == "/donor"
    verify(donor_secret, request.headers, request.body)
    found = {row.subscription_id: row for row in await rows(deliveries)}
    assert found.pop(donor.id).status == SUCCEEDED
    assert {(row.status, row.attempts, row.last_error) for row in found.values()} == {
        (FAILED, 0, KEY_LOST)
    }
    assert all(row.finished_at is not None for row in found.values())
    assert len(await rows(attempts)) == 1
    assert KEY_LOST == (
        "No key decrypts the subscription's secret: SECRET_KEY changed without "
        "the old key in SECRET_KEY_FALLBACKS. Put it back, or rotate the secret, "
        "then replay. Nothing was sent."
    )
    flush_reports()  # Reporters run on their own thread.
    assert sorted(str(context["subscription_id"]) for _, context in reported) == sorted(
        str(subscription_id) for subscription_id in found
    )
    assert {text for text, _ in reported} == {"No key decrypts the value."}
    delivered = {row.id: row.subscription_id for row in await rows(deliveries)}
    assert sorted(caplog.messages) == sorted(
        f"Webhook delivery {delivery_id} to subscription {subscription_id} "
        f"failed: {KEY_LOST}"
        for delivery_id, subscription_id in delivered.items()
        if subscription_id != donor.id
    )
    assert {record.levelno for record in caplog.records} == {logging.ERROR}


async def test_deliveries_encrypt_fallback_key_secrets_again(
    hooks: Receiver, caplog: pytest.LogCaptureFixture
) -> None:
    """After SECRET_KEY changes, with the old key in fallbacks, deliveries verify.

    And move the secrets under the new key, so they outlive the fallback.
    """
    with override_settings(SECRET_KEY=OLD_KEY):
        subscription, old = await subscribe(hooks.url(), "note.written")
        new = await rotate(subscription.id)
        before = await stored_secrets(subscription.id)
    with (
        override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]),
        caplog.at_level(logging.DEBUG, "domainkit"),
    ):
        await write_note()
        await drain(delayed=True)
        after = await stored_secrets(subscription.id)
        await write_note()
        await drain(delayed=True)
        assert await stored_secrets(subscription.id) == after  # Done once.
    with override_settings(SECRET_KEY=NEW_KEY):  # The fallback is gone.
        await write_note()
        await drain(delayed=True)
        for sealed, secret in zip(after, (new, old), strict=True):
            assert sealed is not None
            assert secret_box().decrypt(sealed, associated=subscription.id.bytes) == (
                secret.encode(),
                True,
            )

    assert after[0] != before[0]
    assert after[1] != before[1]
    assert len(hooks.requests) == 3
    for request in hooks.requests:
        assert len(request.headers["webhook-signature"].split()) == 2
        for secret in (new, old):
            verify(secret, request.headers, request.body)
    assert {row.status for row in await rows(deliveries)} == {SUCCEEDED}
    assert (
        f"Encrypted the secrets of webhook subscription {subscription.id} again, "
        "under SECRET_KEY."
    ) in caplog.messages
    for secret in (old, new):
        assert secret not in caplog.text
        assert secret.removeprefix("whsec_") not in caplog.text


async def test_expired_previous_secrets_are_left_alone(hooks: Receiver) -> None:
    """A previous secret that no longer signs is neither decrypted nor rewritten."""
    with override_settings(SECRET_KEY=OLD_KEY):
        subscription, _ = await subscribe(hooks.url(), "note.written")
        new = await rotate(subscription.id)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(subscriptions).values(
                previous_secret=b"unreadable",
                previous_secret_expires_at=datetime.now(UTC),
            )
        )
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        await write_note()
        await drain(delayed=True)

    (request,) = hooks.requests
    assert len(request.headers["webhook-signature"].split()) == 1
    verify(new, request.headers, request.body)
    assert (await stored_secrets(subscription.id))[1] == b"unreadable"


async def test_a_lost_key_fails_deliveries_until_the_secret_is_rotated(
    hooks: Receiver,
) -> None:
    """SECRET_KEY changed with no fallback: deliveries fail, and nothing is sent.

    A rotation drops the secret no key decrypts; a replay then goes out.
    """
    with override_settings(SECRET_KEY=OLD_KEY):
        subscription, _ = await subscribe(hooks.url(), "note.written")
    await write_note()  # Under the tests' SECRET_KEY: OLD_KEY is lost.

    await drain(delayed=True)

    (failed,) = await rows(deliveries)
    assert (failed.status, failed.attempts, failed.last_error) == (
        FAILED,
        0,
        KEY_LOST,
    )
    assert hooks.requests == []
    secret = await rotate(subscription.id)
    assert (await stored_secrets(subscription.id))[1] is None
    assert await delivery_module.replay([failed.id]) == [failed.id]
    await drain(delayed=True)
    (request,) = hooks.requests
    assert len(request.headers["webhook-signature"].split()) == 1
    verify(secret, request.headers, request.body)
    assert (await rows(deliveries))[0].status == SUCCEEDED


async def test_reencryption_leaves_secrets_a_rotation_replaced(hooks: Receiver) -> None:
    """Only the value that was decrypted is replaced, in its own row."""
    subscription, _ = await subscribe(hooks.url(), "*")
    other, _ = await subscribe(hooks.url(), "*")
    sealed, _ = await stored_secrets(subscription.id)
    engine = connections.engine()
    async with engine.begin() as connection:  # A copy, which would not decrypt.
        await connection.execute(
            update(subscriptions)
            .where(subscriptions.c.id == other.id)
            .values(secret=sealed)
        )

    await delivery_module._reseal(engine, subscription.id, {"secret": (b"x", b"new")})
    assert await stored_secrets(subscription.id) == (sealed, None)
    await delivery_module._reseal(engine, subscription.id, {"secret": (sealed, b"new")})
    assert await stored_secrets(subscription.id) == (b"new", None)
    assert await stored_secrets(other.id) == (sealed, None)


async def test_finishing_a_delivery_closes_that_pending_one_only(
    hooks: Receiver,
) -> None:
    """Other deliveries, and finished ones, are left as they are."""
    await subscribe(hooks.url(), "*")
    await subscribe(hooks.url(), "*")
    await queue_deliveries("x", {}, key="k", occurred_at=datetime.now(UTC))
    first, second = (row.id for row in await rows(deliveries))
    engine = connections.engine()
    async with engine.begin() as connection:
        await connection.execute(
            update(deliveries).where(deliveries.c.id == second).values(status=SUCCEEDED)
        )

    await delivery_module._finish(engine, second, CANCELLED, "late")
    await delivery_module._finish(engine, first, CANCELLED, "gone")

    found = {row.id: (row.status, row.last_error) for row in await rows(deliveries)}
    assert found == {first: (CANCELLED, "gone"), second: (SUCCEEDED, None)}


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
    """The periodic task deletes what is older than RETENTION_SECONDS (30 days)."""
    from domainkit.contrib.messaging import registry as tasks

    await subscribe(hooks.url(), "*")
    for key in ("old", "recent"):
        await queue_deliveries("x", {}, key=key, occurred_at=datetime.now(UTC))
    await drain(delayed=True)
    old, _ = await rows(deliveries)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(deliveries)
            .where(deliveries.c.id == old.id)
            .values(finished_at=datetime.now(UTC) - timedelta(days=31))
        )
    spec = tasks.get_task("domainkit.contrib.webhooks.purge")
    assert spec is not None
    assert spec.every == timedelta(hours=1)

    await spec.function()

    assert [row.id for row in await rows(deliveries)] != [old.id]
    assert len(await rows(deliveries)) == 1


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


def test_webhooks_need_a_secret_key_and_cryptography(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription secrets are encrypted under a key derived from SECRET_KEY."""
    installed = {
        "INSTALLED_DOMAINS": [MESSAGING, WEBHOOKS],
        "DATABASES": {"default": {"URL": database_url}},
        "ALPHA": UNSET,
    }
    registry.clear()
    with (
        override_settings(**installed, SECRET_KEY=None),
        pytest.raises(ImproperlyConfigured) as no_key,
    ):
        setup()
    registry.clear()
    monkeypatch.setitem(sys.modules, "cryptography", None)
    with (
        override_settings(**installed),
        pytest.raises(ImproperlyConfigured) as no_cryptography,
    ):
        setup()
    registry.clear()

    assert str(no_key.value) == (
        "domainkit.contrib.webhooks encrypts subscription secrets under a key "
        "derived from SECRET_KEY: set SECRET_KEY."
    )
    assert str(no_cryptography.value) == (
        "domainkit.contrib.webhooks encrypts subscription secrets with "
        'cryptography: pip install "domainkit[webhooks]".'
    )


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
