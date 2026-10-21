"""RabbitMQ: the adapter, TLS, a least-privilege user, and brokered messaging.

Every test runs on ``fake_amqp``, an in-memory RabbitMQ, so they all run
without a server. Set ``DOMAINKIT_TEST_RABBITMQ_URL``
(``amqp://user:pw@host:5672/``) and ``DOMAINKIT_TEST_RABBITMQ_TLS_URL``
with ``DOMAINKIT_TEST_RABBITMQ_CA`` to run them on real servers too, as CI
does; ``DOMAINKIT_TEST_RABBITMQ_MANAGEMENT_URL`` (``http://user:pw@host:
15672``) enables the least-privilege user test. Each test uses its own
prefix and deletes what it declared.
"""

import asyncio
import base64
import json
import os
import urllib.parse
import urllib.request
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Final, cast

import aio_pika
import pytest
from aio_pika.abc import AbstractConnection
from aio_pika.exceptions import ChannelPreconditionFailed

from domainkit import setup
from domainkit.apps import registry
from domainkit.broker import BrokerRefused, BrokerUnavailable, Delivery, brokers
from domainkit.broker.rabbitmq import RabbitMQBroker, ssl_context
from domainkit.conf import BrokerConfig, override_settings
from domainkit.contrib.messaging import MessagingSettings, Worker, enqueue
from domainkit.contrib.messaging.dispatch import BrokerWorkers
from domainkit.contrib.messaging.tables import DONE, messages
from domainkit.db import connections
from domainkit_tests import test_messaging_runtime as runtime
from domainkit_tests.fake_amqp import FakeAMQP
from domainkit_tests.test_messaging import (
    ALPHA,
    FAST,
    MESSAGING,
    SLOW,
    outbox,
    ran,
    remember,
    send_email,
)

SERVERS: Final = {
    "fake": "amqp://domainkit:domainkit@fake-rabbitmq:5672/",
    "rabbitmq": os.environ.get("DOMAINKIT_TEST_RABBITMQ_URL", ""),
    "rabbitmq-tls": os.environ.get("DOMAINKIT_TEST_RABBITMQ_TLS_URL", ""),
}
CA: Final = os.environ.get("DOMAINKIT_TEST_RABBITMQ_CA")
MANAGEMENT: Final = os.environ.get("DOMAINKIT_TEST_RABBITMQ_MANAGEMENT_URL", "")

pytestmark = pytest.mark.anyio

type Connect = Callable[..., Awaitable[AbstractConnection]]


class Server:
    """A test server: its settings, and how to connect to it directly."""

    def __init__(self, name: str) -> None:
        """Settings for ``name`` with a prefix of the test's own."""
        url = SERVERS[name]
        if not url:
            pytest.skip(f"set DOMAINKIT_TEST_{name.upper().replace('-', '_')}_URL")
        self.name = name
        self.options: dict[str, object] = {"URL": url, "PREFIX": f"t{uuid.uuid4().hex}"}
        if url.startswith("amqps://"):
            self.options["TLS_CA"] = CA
        self.fake = FakeAMQP() if name == "fake" else None

    @property
    def config(self) -> BrokerConfig:
        """The broker settings."""
        return BrokerConfig.model_validate(self.options)

    @property
    def connect(self) -> Connect:
        """How the adapter connects: the fake, or aio-pika."""
        if self.fake is not None:
            return cast(Connect, self.fake.connect)
        return aio_pika.connect_robust

    def broker(self, **options: object) -> RabbitMQBroker:
        """A broker on this server."""
        config = BrokerConfig.model_validate({**self.options, **options})
        return RabbitMQBroker(config, connect=self.connect)

    async def clean(self, queues: list[str]) -> None:
        """Delete what a test declared (real servers keep it otherwise)."""
        if self.fake is not None:
            return
        config = self.config
        connection = await aio_pika.connect_robust(
            config.URL.get_secret_value(), ssl_context=ssl_context(config)
        )
        async with connection:
            channel = await connection.channel()
            for queue in queues:
                await channel.queue_delete(f"{config.PREFIX}.{queue}")
            await channel.exchange_delete(f"{config.PREFIX}.work")


@pytest.fixture(params=list(SERVERS))
async def server(request: pytest.FixtureRequest) -> AsyncIterator[Server]:
    """Each server: the fake always, real ones when configured."""
    opened = Server(request.param)
    yield opened
    await opened.clean(["default", "emails"])


class Inbox:
    """Collect deliveries, acknowledging them unless told otherwise."""

    def __init__(self, *, requeue_first: bool = False) -> None:
        """Requeue the first delivery when ``requeue_first``."""
        self.requeue_first = requeue_first
        self.received: list[tuple[str, bytes, str]] = []
        self.changed = asyncio.Event()

    async def __call__(self, delivery: Delivery) -> None:
        """Record ``delivery`` and answer it."""
        if self.requeue_first:
            self.requeue_first = False
            await delivery.requeue()
            return
        self.received.append((delivery.queue, delivery.body, delivery.message_id))
        await delivery.ack()
        self.changed.set()

    async def wait(self, count: int) -> list[tuple[str, bytes, str]]:
        """Return once ``count`` deliveries arrived."""
        async with asyncio.timeout(10):
            while len(self.received) < count:
                self.changed.clear()
                await self.changed.wait()
        return self.received


async def test_messages_reach_one_consumer_each(server: Server) -> None:
    """Published messages arrive on their queue, with their ids, once each."""
    broker = server.broker()
    try:
        await broker.declare(["default", "emails"])
        inbox = Inbox()
        subscription = await broker.consume(["default", "emails"], 2, inbox)
        for number, queue in enumerate(["default", "emails", "default"]):
            await broker.publish(queue, b"%d" % number, message_id=f"m{number}")
        received = await inbox.wait(3)
        await subscription.cancel()
    finally:
        await broker.close()

    assert sorted(received) == [
        ("default", b"0", "m0"),
        ("default", b"2", "m2"),
        ("emails", b"1", "m1"),
    ]


async def test_requeued_messages_come_back(server: Server) -> None:
    """A requeued message is delivered again."""
    broker = server.broker()
    try:
        await broker.declare(["default"])
        inbox = Inbox(requeue_first=True)
        subscription = await broker.consume(["default"], 1, inbox)
        await broker.publish("default", b"once", message_id="m")
        received = await inbox.wait(1)
        await subscription.cancel()
    finally:
        await broker.close()

    assert received == [("default", b"once", "m")]


async def test_answers_the_broker_cannot_take_are_broker_errors(
    server: Server,
) -> None:
    """A delivery answered twice, or once its channel closed, is a broker error.

    aio-pika raises errors of its own for both, a ``RuntimeError`` once the
    channel closed (a restart, a dropped connection); a worker must only
    ever see ``BrokerError``s.
    """
    broker = server.broker()
    held: list[Delivery] = []
    arrived = asyncio.Event()

    async def hold(delivery: Delivery) -> None:
        held.append(delivery)
        if len(held) == 2:
            arrived.set()

    try:
        await broker.declare(["default"])
        subscription = await broker.consume(["default"], 2, hold)
        await broker.publish("default", b"1", message_id="m1")
        await broker.publish("default", b"2", message_id="m2")
        async with asyncio.timeout(10):
            await arrived.wait()
        first, second = held
        await first.ack()
        with pytest.raises(BrokerUnavailable, match=r"processed|settled once"):
            await first.requeue()
        await subscription.cancel()
    finally:
        await broker.close()
    for answer in (second.ack, second.requeue):
        with pytest.raises(BrokerUnavailable, match="ChannelInvalidStateError"):
            await answer()


async def test_unroutable_messages_and_changed_queues_are_refused(
    server: Server,
) -> None:
    """No queue for a message, or a queue declared differently, is a refusal."""
    broker = server.broker()
    try:
        await broker.declare(["default"])
        with pytest.raises(BrokerRefused, match="refused the message"):
            await broker.publish("emails", b"{}", message_id="m")
        await broker.check()
    finally:
        await broker.close()

    config = server.config
    connection = await server.connect(
        config.URL.get_secret_value(), ssl_context=ssl_context(config)
    )
    try:
        channel = await connection.channel()
        with pytest.raises(ChannelPreconditionFailed):
            # A classic queue under the quorum queue's name.
            await channel.declare_queue(
                f"{config.PREFIX}.default", durable=True, arguments={}
            )
        # Someone made "emails" a classic queue: the worker refuses to use it.
        await (await connection.channel()).declare_queue(
            f"{config.PREFIX}.emails", durable=True, arguments={}
        )
    finally:
        await connection.close()
    broker = server.broker()
    try:
        with pytest.raises(BrokerRefused, match="PRECONDITION_FAILED"):
            await broker.declare(["emails"])
    finally:
        await broker.close()


@pytest.mark.skipif(
    not SERVERS["rabbitmq-tls"], reason="set DOMAINKIT_TEST_RABBITMQ_TLS_URL"
)
async def test_tls_needs_a_trusted_certificate() -> None:
    """Without the server's CA, the system's CAs refuse its certificate."""
    broker = Server("rabbitmq-tls").broker(TLS_CA=None, CONNECT_TIMEOUT=5)
    with pytest.raises(BrokerUnavailable, match="CERTIFICATE_VERIFY_FAILED"):
        await broker.check()
    await broker.close()


def management(method: str, path: str, body: object | None = None) -> None:
    """Call RabbitMQ's management API with the credentials in its URL."""
    parts = urllib.parse.urlsplit(MANAGEMENT)
    credentials = f"{parts.username}:{parts.password}".encode()
    request = urllib.request.Request(  # noqa: S310 - a test server's URL.
        f"{parts.scheme}://{parts.hostname}:{parts.port}/api/{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Basic {base64.b64encode(credentials).decode()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10):  # noqa: S310
        pass


@pytest.mark.skipif(
    not (MANAGEMENT and SERVERS["rabbitmq"]),
    reason="set DOMAINKIT_TEST_RABBITMQ_URL and _MANAGEMENT_URL",
)
async def test_a_least_privilege_user_is_enough() -> None:
    """The permissions in docs/contrib.md cover the worker; other names are refused."""
    server = Server("rabbitmq")
    prefix = str(server.options["PREFIX"])
    user, password = f"u{uuid.uuid4().hex[:12]}", uuid.uuid4().hex
    pattern = f"^{prefix}\\."
    await asyncio.to_thread(
        management, "PUT", f"users/{user}", {"password": password, "tags": ""}
    )
    await asyncio.to_thread(
        management,
        "PUT",
        f"permissions/%2F/{user}",
        {"configure": pattern, "write": pattern, "read": pattern},
    )
    url = urllib.parse.urlsplit(SERVERS["rabbitmq"])
    restricted = url._replace(
        netloc=f"{user}:{password}@{url.hostname}:{url.port}"
    ).geturl()
    broker = server.broker(URL=restricted)
    other = server.broker(URL=restricted, PREFIX=f"{prefix}x")
    try:
        await broker.declare(["default"])
        inbox = Inbox()
        subscription = await broker.consume(["default"], 1, inbox)
        await broker.publish("default", b"ok", message_id="m")
        assert await inbox.wait(1) == [("default", b"ok", "m")]
        await subscription.cancel()
        with pytest.raises(BrokerRefused, match="ACCESS_REFUSED"):
            await other.declare(["default"])
    finally:
        await broker.close()
        await other.close()
        await server.clean(["default"])
        await asyncio.to_thread(management, "DELETE", f"users/{user}")


@pytest.fixture
def brokered(
    server: Server, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Server]:
    """Messaging on an empty database, through ``server``."""
    monkeypatch.setattr(
        brokers,
        "_connect",
        lambda config: RabbitMQBroker(config, connect=server.connect),
    )
    ran.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        BROKERS={"work": server.options},
        MESSAGING={**FAST, "BROKER": "work"},
    ):
        setup()
        yield server
    registry.clear()


async def test_brokered_messaging_end_to_end(brokered: Server) -> None:
    """The outbox relays to RabbitMQ; workers run what it delivers, once."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)
    await enqueue(send_email, to="ada@example.com")
    await enqueue(remember, value=uuid.UUID(int=1))
    stop = asyncio.Event()
    config = registry.settings(MessagingSettings)
    running = asyncio.create_task(
        BrokerWorkers([Worker()], ["default", "emails"], config).run(stop)
    )
    try:
        for _ in range(500):
            rows = await outbox()
            if all(row["status"] == DONE for row in rows):
                break
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await running
        await brokers.close_all()
        await connections.close_all()

    assert [row["status"] for row in rows] == [DONE, DONE]
    assert [row["attempts"] for row in rows] == [1, 1]
    assert ("email", "ada@example.com") in ran
    assert (
        sum(
            1
            for entry in ran
            if isinstance(entry, tuple) and entry[0] == uuid.UUID(int=1)
        )
        == 1
    )


async def test_work_in_flight_never_exceeds_the_budget(brokered: Server) -> None:
    """Each queue's consumer has its own prefetch; the process still runs one at once.

    RabbitMQ applies the prefetch per consumer on quorum queues, so two
    queues with a prefetch of one hand out two messages; the worker's
    budget of one runs them one after the other.
    """
    gate = runtime.gate = runtime.Gate()
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)
    with override_settings(
        MESSAGING={**FAST, **SLOW, "BROKER": "work", "CONCURRENCY": 1}
    ):
        for name in ("d1", "d2"):
            await enqueue(runtime.at_gate, name=name)
        for name in ("e1", "e2"):
            await enqueue(runtime.at_gate_emails, name=name)
        config = registry.settings(MessagingSettings)
        budget = runtime.WatchedBudget(1)
        stop = asyncio.Event()
        workers = BrokerWorkers(
            [Worker(config)], ["default", "emails"], config, budget=budget
        )
        running = asyncio.create_task(workers.run(stop))
        try:
            await gate.until(lambda: len(gate.started) == 1)
            await budget.until_waiting(1)  # The other queue's delivery waits.
            assert gate.running == 1
            gate.open()
            await gate.until(lambda: len(gate.finished) == 4)
        finally:
            gate.open()
            stop.set()
            async with asyncio.timeout(runtime.WAIT):
                await running
            await brokers.close_all()

    rows = await outbox()
    await connections.close_all()
    assert gate.peak == 1
    assert sorted(gate.finished) == ["d1", "d2", "e1", "e2"]
    assert [row["status"] for row in rows] == [DONE] * 4
