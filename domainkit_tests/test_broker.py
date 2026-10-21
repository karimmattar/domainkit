"""Brokers without a server: settings, the in-memory broker, the RabbitMQ adapter.

The adapter runs here on ``fake_amqp``, a small in-memory RabbitMQ behind
aio-pika's API; ``test_rabbitmq.py`` runs it on real servers too.
"""

import asyncio
import datetime as dt
import io
import ssl
import threading
from pathlib import Path
from typing import Any, Final, cast

import pytest
from aio_pika import DeliveryMode
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from domainkit import setup
from domainkit.apps import registry
from domainkit.broker import (
    Broker,
    BrokerHandler,
    BrokerRefused,
    BrokerUnavailable,
    Delivery,
    MemoryBroker,
    brokers,
    rabbitmq,
)
from domainkit.broker.rabbitmq import QUEUE_ARGUMENTS, RabbitMQBroker, ssl_context
from domainkit.conf import UNSET, BrokerConfig, Settings, override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import CommandError
from domainkit_tests.fake_amqp import FakeAMQP
from domainkit_tests.settings import SECRET_KEY

pytestmark = pytest.mark.anyio

RABBIT: Final = {"URL": "amqp://billing:pw@localhost:5672/billing", "PREFIX": "billing"}
"""A RabbitMQ broker the tests reach through ``fake_amqp``."""


def rabbit(server: FakeAMQP, **options: object) -> RabbitMQBroker:
    """A RabbitMQ broker on the fake ``server``."""
    config = BrokerConfig.model_validate({**RABBIT, **options})
    return RabbitMQBroker(config, connect=cast(Any, server.connect))


async def test_racing_first_uses_share_one_connection() -> None:
    """Callers racing to open the broker get one connection and one channel."""
    server = FakeAMQP()
    channels: list[object] = []

    async def slow_connect(url: str, **options: Any) -> object:  # noqa: ANN401
        await asyncio.sleep(0)  # A real connect yields while it handshakes.
        connection = await server.connect(url, **options)
        opened = connection.channel

        async def channel(**options: Any) -> object:  # noqa: ANN401
            await asyncio.sleep(0)
            made = await opened(**options)
            channels.append(made)
            return made

        connection.channel = channel  # type: ignore[method-assign,assignment]
        return connection

    config = BrokerConfig.model_validate(RABBIT)
    broker = RabbitMQBroker(config, connect=cast(Any, slow_connect))

    await asyncio.gather(*(broker.check() for _ in range(5)))
    await broker.close()

    assert (len(server.connections), len(channels)) == (1, 1)
    [channel] = channels
    # Publishes wait for RabbitMQ's confirmation; unroutable ones raise.
    assert (channel.publisher_confirms, channel.on_return_raises) == (True, True)  # type: ignore[attr-defined]


async def test_amqps_connections_trust_the_configured_ca(tmp_path: Path) -> None:
    """The TLS context ssl_context() builds is the one the connection uses."""
    server = FakeAMQP()
    cert, _ = certificate(tmp_path)
    url = "amqps://billing:pw@mq.internal/billing"
    broker = rabbit(server, URL=url, TLS_CA=str(cert))

    await broker.check()
    await broker.close()

    [(_, options)] = server.connections
    assert isinstance(options["ssl_context"], ssl.SSLContext)
    assert options["ssl_context"].verify_mode == ssl.CERT_REQUIRED


async def test_tls_contexts_are_built_off_the_loop_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each broker gets the process's one context, built on a worker thread."""
    built_on: list[str] = []
    building = rabbitmq.ssl_context

    def watched(config: BrokerConfig) -> ssl.SSLContext | None:
        built_on.append(threading.current_thread().name)
        return building(config)

    monkeypatch.setattr(rabbitmq, "ssl_context", watched)
    server = FakeAMQP()
    cert, _ = certificate(tmp_path)
    url = "amqps://billing:pw@mq.internal/billing"
    brokers = [rabbit(server, URL=url, TLS_CA=str(cert)) for _ in range(2)]

    for broker in brokers:
        await broker.check()
        await broker.close()

    first, second = (options["ssl_context"] for _, options in server.connections)
    assert first is second
    assert threading.current_thread().name not in built_on
    assert len(built_on) == 2


class Inbox:
    """Collect deliveries; ``wait`` until ``count`` arrived."""

    def __init__(self, *, ack: bool = True) -> None:
        """Acknowledge each delivery unless ``ack`` is False."""
        self.ack = ack
        self.deliveries: list[Delivery] = []
        self._arrived = asyncio.Event()
        self._expected = 0

    async def __call__(self, delivery: Delivery) -> None:
        """Record ``delivery``."""
        self.deliveries.append(delivery)
        if self.ack:
            await delivery.ack()
        if len(self.deliveries) >= self._expected:
            self._arrived.set()

    async def wait(self, count: int) -> list[Delivery]:
        """Return once ``count`` deliveries arrived."""
        self._expected = count
        if len(self.deliveries) < count:
            self._arrived.clear()
            async with asyncio.timeout(5):
                while len(self.deliveries) < count:
                    await self._arrived.wait()
                    self._arrived.clear()
        return self.deliveries[:count]


# Settings ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"URL": "kafka://host"}, "a broker URL starts with"),
        ({"URL": "amqp:///vhost", "PREFIX": "x"}, "an AMQP URL needs a host"),
        ({"URL": "amqps://mq.internal/"}, "RabbitMQ brokers need a PREFIX"),
        ({"URL": "memory://", "PREFIX": "a b"}, "String should match pattern"),
        ({"URL": "amqp://h/", "PREFIX": "x", "TLS_CA": "ca.pem"}, "need an amqps"),
        (
            {"URL": "amqps://h/", "PREFIX": "x", "TLS_KEY": "k.pem"},
            "TLS_CERT and TLS_KEY go together",
        ),
    ],
)
def test_broker_settings_are_checked(options: dict[str, object], message: str) -> None:
    """Unusable broker settings fail at startup."""
    with pytest.raises(ValidationError, match=message):
        BrokerConfig.model_validate(options)


def test_broker_url_properties() -> None:
    """Network, certificate checks, and credentials are read from the URL."""
    local = BrokerConfig(URL="amqp://u:p@localhost/", PREFIX="x")
    remote = BrokerConfig(URL="amqps://billing:pw@mq.internal/billing", PREFIX="x")
    lax = BrokerConfig(URL="amqps://u:p@mq.internal/?no_verify_ssl=1", PREFIX="x")
    guest = BrokerConfig(URL="amqps://guest:guest@mq.internal/", PREFIX="x")
    certified = BrokerConfig(
        URL="amqps://mq.internal/",
        PREFIX="x",
        TLS_CERT=Path("c.pem"),
        TLS_KEY=Path("k.pem"),
    )
    memory = BrokerConfig(URL="memory://")

    assert (local.is_network(), remote.is_network(), memory.is_network()) == (
        False,
        True,
        False,
    )
    assert (remote.verifies_tls(), lax.verifies_tls()) == (True, False)
    assert (remote.has_credentials(), guest.has_credentials()) == (True, False)
    assert certified.has_credentials()
    assert (remote.scheme, memory.scheme) == ("amqps", "memory")


@pytest.mark.parametrize(
    ("broker", "message"),
    [
        ({"URL": "memory://"}, "memory:// brokers serve one process"),
        ({"URL": "amqp://billing:pw@mq.internal/", "PREFIX": "b"}, "need amqps://"),
        (
            {"URL": "amqps://billing:pw@mq.internal/?no_verify_ssl=1", "PREFIX": "b"},
            "must verify the server certificate",
        ),
        ({"URL": "amqps://mq.internal/", "PREFIX": "b"}, "not guest"),
    ],
)
def test_deployed_brokers_need_tls_and_a_user(
    broker: dict[str, object], message: str
) -> None:
    """Staging and production refuse brokers that are local, plain, or anonymous."""
    base = {
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": ["api.example.com"],
        "SECRET_KEY": SECRET_KEY,
    }

    with pytest.raises(ValidationError, match=f"{message}.*BROKERS \\['work'\\]"):
        Settings.model_validate({**base, "BROKERS": {"work": broker}})

    fine = {"URL": "amqps://billing:pw@mq.internal/billing", "PREFIX": "billing"}
    assert Settings.model_validate({**base, "BROKERS": {"work": fine}}).BROKERS


# The in-memory broker ----------------------------------------------------------


async def test_memory_broker_hands_each_message_to_one_consumer() -> None:
    """Published messages reach one consumer each, within the prefetch."""
    broker = MemoryBroker()
    with pytest.raises(BrokerRefused, match="No queue 'default' takes"):
        await broker.publish("default", b"{}", message_id="early")
    await broker.declare(["default"])
    for number in range(3):
        await broker.publish("default", b"%d" % number, message_id=f"m{number}")
    assert broker.pending("default") == 3

    held = Inbox(ack=False)
    subscription = await broker.consume(["default"], 2, held)
    first, second = await held.wait(2)
    await asyncio.sleep(0.01)
    assert len(held.deliveries) == 2  # The prefetch holds the third back.

    await first.ack()
    (third,) = (await held.wait(3))[2:]
    await second.requeue()
    redelivered = (await held.wait(4))[3]
    await third.ack()
    await redelivered.ack()
    await subscription.cancel()

    assert [d.message_id for d in held.deliveries] == ["m0", "m1", "m2", "m1"]
    assert held.deliveries[0].queue == "default"
    assert broker.pending("default") == 0


async def test_only_the_first_answer_counts() -> None:
    """A message is acknowledged or requeued once; later answers change nothing."""
    broker = MemoryBroker()
    await broker.declare(["default"])
    await broker.publish("default", b"{}", message_id="m")
    held = Inbox(ack=False)
    subscription = await broker.consume(["default"], 1, held)
    (only,) = await held.wait(1)

    await only.ack()
    await only.requeue()
    await asyncio.sleep(0.01)
    await subscription.cancel()

    assert len(held.deliveries) == 1
    assert broker.pending("default") == 0
    await broker.check()
    await broker.close()


# The handler --------------------------------------------------------------------


def test_rabbitmq_brokers_are_built_lazily() -> None:
    """The default factory builds a RabbitMQ broker without connecting."""
    from domainkit.broker.handler import rabbitmq_broker

    broker = rabbitmq_broker(BrokerConfig.model_validate(RABBIT))

    assert isinstance(broker, RabbitMQBroker)
    assert (broker.exchange_name, broker.queue_name("emails")) == (
        "billing.work",
        "billing.emails",
    )


def test_handler_gives_each_loop_its_brokers() -> None:
    """Brokers are per loop; changed settings get a new one, closed with the loop."""
    server = FakeAMQP()
    handler = BrokerHandler(
        connect=lambda config: RabbitMQBroker(config, connect=cast(Any, server.connect))
    )

    async def use() -> tuple[Broker, Broker]:
        memory, amqp = handler["memory"], handler["rabbit"]
        assert handler["memory"] is memory
        await amqp.check()
        with override_settings(
            BROKERS={
                "memory": {"URL": "memory://"},
                "rabbit": {**RABBIT, "PREFIX": "b2"},
            }
        ):
            assert handler["rabbit"] is not amqp
        await handler.close_all()
        return memory, amqp

    with override_settings(BROKERS={"memory": {"URL": "memory://"}, "rabbit": RABBIT}):
        first = asyncio.run(use())
        second = asyncio.run(use())
        with pytest.raises(RuntimeError, match="needs a running event loop"):
            handler["memory"]

    assert first[0] is not second[0]
    assert isinstance(first[0], MemoryBroker)
    assert len(server.connections) == 2
    with pytest.raises(ImproperlyConfigured, match="BROKERS has no 'nope' alias"):
        handler.config("nope")


# The RabbitMQ adapter on the fake -----------------------------------------------------


async def test_rabbitmq_topology_and_publishing() -> None:
    """Durable exchange, quorum queues, persistent mandatory confirmed messages."""
    server = FakeAMQP()
    broker = rabbit(server, PUBLISH_TIMEOUT=3, CONNECT_TIMEOUT=2)
    await broker.declare(["default", "emails"])
    await broker.publish("emails", b'{"id": 1}', message_id="m1")

    ((url, options),) = server.connections
    assert url == RABBIT["URL"]
    assert options["timeout"] == 2
    assert options["ssl_context"] is None
    assert options["client_properties"] == {"connection_name": "domainkit billing"}
    assert server.exchanges == {"billing.work": True}
    assert set(server.queues) == {"billing.default", "billing.emails"}
    assert server.queues["billing.emails"].arguments == QUEUE_ARGUMENTS
    routing_key, message, mandatory, timeout = server.published[0]
    assert (routing_key, mandatory, timeout) == ("emails", True, 3)
    assert (message.body, message.message_id) == (b'{"id": 1}', "m1")
    assert message.delivery_mode == DeliveryMode.PERSISTENT
    assert message.content_type == "application/json"
    assert server.last_channel is not None


async def test_rabbitmq_failures_are_broker_errors() -> None:
    """Unroutable, refused, unconfirmed, and unreachable each have their error."""
    server = FakeAMQP()
    broker = rabbit(server)
    await broker.declare(["default"])

    with pytest.raises(BrokerRefused, match="refused the message"):
        await broker.publish("nowhere", b"{}", message_id="m")
    server.refuse = True
    with pytest.raises(BrokerRefused):
        await broker.publish("default", b"{}", message_id="m")
    server.refuse, server.timeout = False, True
    with pytest.raises(BrokerUnavailable, match="TimeoutError"):
        await broker.publish("default", b"{}", message_id="m")
    server.timeout, server.down = False, True
    with pytest.raises(BrokerUnavailable, match="ChannelClosed"):
        await broker.publish("default", b"{}", message_id="m")
    with pytest.raises(BrokerUnavailable, match="Connection refused"):
        await rabbit(server).check()


async def test_rabbitmq_consumers_respect_prefetch_and_requeue() -> None:
    """Consumers get messages within the prefetch; requeued ones come back."""
    server = FakeAMQP()
    broker = rabbit(server)
    held = Inbox(ack=False)
    subscription = await broker.consume(["default"], 1, held)
    await broker.publish("default", b"1", message_id="m1")
    await broker.publish("default", b"2", message_id="m2")

    (first,) = await held.wait(1)
    await asyncio.sleep(0.01)
    assert len(held.deliveries) == 1
    await first.requeue()
    again = (await held.wait(2))[1]
    await again.ack()
    second = (await held.wait(3))[2]
    await second.ack()
    await subscription.cancel()
    await broker.close()
    await broker.close()  # Closing twice is harmless.

    assert [d.message_id for d in held.deliveries] == ["m1", "m1", "m2"]
    assert server.queues["billing.default"].consumers == {}


def certificate(directory: Path) -> tuple[Path, Path]:
    """Write a throwaway self-signed certificate and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_tls_contexts_verify_with_the_configured_files(tmp_path: Path) -> None:
    """TLS_CA is trusted, TLS_CERT is presented; verification always stays on."""
    cert, key = certificate(tmp_path)
    base = {"URL": "amqps://u:p@mq.internal/", "PREFIX": "x"}
    with_ca = ssl_context(BrokerConfig.model_validate({**base, "TLS_CA": cert}))
    mutual = ssl_context(
        BrokerConfig.model_validate({**base, "TLS_CERT": cert, "TLS_KEY": key})
    )

    assert ssl_context(BrokerConfig.model_validate(base)) is None
    assert with_ca is not None
    assert with_ca.check_hostname
    assert len(with_ca.get_ca_certs()) == 1
    assert mutual is not None
    assert mutual.check_hostname


# The broker command -----------------------------------------------------------------


def run(*args: str) -> str:
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


def test_broker_command_checks_and_declares(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checking connects to each broker; declaring needs messaging and a broker."""
    server = FakeAMQP()
    monkeypatch.setattr(
        brokers,
        "_connect",
        lambda config: RabbitMQBroker(config, connect=cast(Any, server.connect)),
    )
    with override_settings(BROKERS={"memory": {"URL": "memory://"}, "rabbit": RABBIT}):
        assert run("broker", "check") == "memory: ok\nrabbit: ok\n"
        with pytest.raises(CommandError, match="BROKERS has no 'nope'"):
            run("broker", "check", "nope")
        with pytest.raises(CommandError, match=r"needs domainkit\.contrib\.messaging"):
            run("broker", "declare")
        server.down = True
        with pytest.raises(CommandError, match="Unavailable: rabbit"):
            run("broker", "check", "rabbit")
        server.down = False

    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=["domainkit.contrib.messaging"],
        BROKERS={"rabbit": RABBIT},
        ALPHA=UNSET,
    ):
        setup()
        with pytest.raises(CommandError, match=r"declare needs MESSAGING\.BROKER"):
            run("broker", "declare")
        registry.clear()
        with override_settings(MESSAGING={"BROKER": "rabbit"}):
            setup()
            declared = run("broker", "declare")
            assert declared.startswith("rabbit: declared ")
            assert "default" in declared
            assert "billing.default" in server.queues
            server.down = True
            with pytest.raises(CommandError, match="rabbit: RabbitMQ is unavailable"):
                run("broker", "declare")
    registry.clear()
