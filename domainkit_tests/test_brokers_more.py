"""Brokers beside RabbitMQ: Redis Streams, NATS JetStream, Kafka, Amazon SQS.

Each is held to the port's contract: a message goes to one consumer,
which answers it once; a requeued one comes again; an unanswered one,
after ``REDELIVER_AFTER``, goes to another. Redis and NATS run as real
servers the tests start (skipped where the binaries are missing); Kafka
and SQS are fakes of their client and API.
"""

import asyncio
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx2
import pytest

from domainkit.broker import BrokerRefused, BrokerUnavailable, Delivery
from domainkit.broker.handler import connect_broker
from domainkit.broker.kafka import KafkaBroker
from domainkit.broker.nats_jetstream import NatsBroker
from domainkit.broker.pull import Fetched, PullBroker, Puller
from domainkit.broker.redis_streams import RedisStreamsBroker
from domainkit.broker.sqs import SqsBroker
from domainkit.conf import BrokerConfig
from domainkit.http import client as client_module
from domainkit.http.client import stub

pytestmark = pytest.mark.anyio


def config(url: str, **options: Any) -> BrokerConfig:  # noqa: ANN401
    """A broker's settings, prefixed ``t``."""
    return BrokerConfig(URL=url, PREFIX="t", **options)


async def exercise(broker: Any, queues: Sequence[str] = ("q", "r")) -> list[str]:  # noqa: ANN401
    """Publish five, requeue one once, answer all: what was handed out."""
    await broker.declare(queues)
    await broker.declare(queues)  # Declaring again changes nothing.
    for number in range(5):
        await broker.publish(
            queues[number % len(queues)],
            f"m{number}".encode(),
            message_id=f"id{number}",
        )
    seen: list[str] = []
    done = asyncio.Event()

    async def handle(delivery: Delivery) -> None:
        seen.append(f"{delivery.queue}:{delivery.body.decode()}:{delivery.message_id}")
        if delivery.body == b"m1" and sum("m1" in s for s in seen) == 1:
            await delivery.requeue()
            await delivery.ack()  # Only the first answer counts.
        else:
            await delivery.ack()
        if len(seen) == 6:
            done.set()

    subscription = await broker.consume(queues, 2, handle)
    try:
        await asyncio.wait_for(done.wait(), 20)
    finally:
        await subscription.cancel()
    return sorted(seen)


EXPECTED = [
    "q:m0:id0",
    "q:m2:id2",
    "q:m4:id4",
    "r:m1:id1",
    "r:m1:id1",
    "r:m3:id3",
]


def free_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve(command: list[str], port: int) -> Iterator[None]:
    """Run a server until the test ends, once its port answers."""
    process = subprocess.Popen(  # noqa: S603 - our own arguments.
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        yield
    finally:
        process.terminate()
        process.wait(5)


@pytest.fixture
def redis_url() -> Iterator[str]:
    """A Redis server of the test's own."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server is not installed")
    port = free_port()
    yield from (
        f"redis://127.0.0.1:{port}/0"
        for _ in serve([binary, "--port", str(port), "--save", ""], port)
    )


@pytest.fixture
def nats_url(tmp_path: Path) -> Iterator[str]:
    """A NATS server with JetStream, of the test's own."""
    binary = shutil.which("nats-server")
    if binary is None:
        pytest.skip("nats-server is not installed")
    port = free_port()
    command = [binary, "-js", "-p", str(port), "-sd", str(tmp_path)]
    yield from (f"nats://127.0.0.1:{port}" for _ in serve(command, port))


async def test_redis_streams_hold_to_the_contract(redis_url: str) -> None:
    """Each message once, a requeued one again; answered ones deleted."""
    broker = RedisStreamsBroker(config(redis_url))
    try:
        assert await exercise(broker) == EXPECTED
        assert await broker.client.xlen("t:q") == 0
        await broker.check()
    finally:
        await broker.close()


async def test_redis_hands_on_what_a_consumer_left(redis_url: str) -> None:
    """Taken and not answered: another consumer claims it after REDELIVER_AFTER."""
    first = RedisStreamsBroker(config(redis_url, REDELIVER_AFTER=0.2))
    second = RedisStreamsBroker(config(redis_url, REDELIVER_AFTER=0.2))
    await first.declare(["q"])
    await first.publish("q", b"lost", message_id="1")
    taken = await (await first.puller(["q"])).fetch(1)
    await asyncio.sleep(0.3)  # Its consumer went away.

    claimed = await (await second.puller(["q"])).fetch(1)

    assert [m.body for m in taken] == [b"lost"]
    assert [(m.body, m.message_id) for m in claimed] == [(b"lost", "1")]
    await first.close()
    await second.close()


async def test_an_unreachable_redis_is_unavailable() -> None:
    """Connection refused: BrokerUnavailable, naming the step."""
    broker = RedisStreamsBroker(
        config(f"redis://127.0.0.1:{free_port()}/0", CONNECT_TIMEOUT=0.5)
    )
    for step in (
        broker.check(),
        broker.publish("q", b"x", message_id="1"),
        broker.declare(["q"]),
    ):
        with pytest.raises(BrokerUnavailable, match="Redis could not be reached"):
            await step
    await broker.close()


async def test_redis_refuses_a_key_of_another_kind(redis_url: str) -> None:
    """A string where the stream would be: BrokerRefused."""
    broker = RedisStreamsBroker(config(redis_url))
    await broker.client.set("t:taken", "a string")
    with pytest.raises(BrokerRefused, match="stream 'taken'"):
        await broker.declare(["taken"])
    await broker.close()


async def test_rediss_passes_its_certificates() -> None:
    """A rediss:// URL hands the CA and the client certificate to redis-py."""
    broker = RedisStreamsBroker(
        BrokerConfig(
            URL="rediss://:pw@cache.internal:6380/0",
            PREFIX="t",
            TLS_CA="ca.pem",
            TLS_CERT="c.pem",
            TLS_KEY="k.pem",
        )
    )
    kwargs = broker.client.connection_pool.connection_kwargs
    assert (kwargs["ssl_ca_certs"], kwargs["ssl_certfile"]) == ("ca.pem", "c.pem")
    await broker.close()


async def test_nats_jetstream_holds_to_the_contract(nats_url: str) -> None:
    """Each message once (an id published twice is stored once), requeue again."""
    broker = NatsBroker(config(nats_url))
    try:
        await broker.declare(["q", "r"])
        await broker.publish("q", b"m0", message_id="id0")  # Before: stored once.
        assert await exercise(broker) == EXPECTED
        await broker.check()
        stranger = NatsBroker(BrokerConfig(URL=nats_url, PREFIX="none"))
        with pytest.raises(BrokerRefused):
            await stranger.publish("q", b"x", message_id="1")
        await stranger.close()
    finally:
        await broker.close()


async def test_nats_hands_on_what_a_consumer_left(nats_url: str) -> None:
    """Not answered within REDELIVER_AFTER: handed out again."""
    broker = NatsBroker(config(nats_url, REDELIVER_AFTER=0.5))
    await broker.declare(["slow"])
    await broker.publish("slow", b"lost", message_id="1")
    puller = await broker.puller(["slow"])
    taken = await puller.fetch(1)
    await asyncio.sleep(0.8)

    again = await puller.fetch(1)

    assert [m.body for m in taken] == [m.body for m in again] == [b"lost"]
    await again[0].handle.ack()
    assert await puller.fetch(1) == []
    await puller.close()
    await broker.close()


async def test_an_unreachable_nats_is_unavailable() -> None:
    """Nothing listening: BrokerUnavailable."""
    broker = NatsBroker(config(f"nats://127.0.0.1:{free_port()}", CONNECT_TIMEOUT=0.5))
    with pytest.raises(BrokerUnavailable, match="NATS could not be reached"):
        await broker.check()


async def test_nats_tls_and_failures_of_its_own(tmp_path: Path) -> None:
    """nats+tls:// connects with tls:// and a context; failures are named."""
    seen: dict[str, Any] = {}

    class Connection:
        def jetstream(self) -> Any:  # noqa: ANN401
            return JetStream()

        async def close(self) -> None:
            seen["closed"] = True

    class JetStream:
        async def account_info(self) -> None:
            raise RuntimeError("down")

        async def publish(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            from nats.errors import TimeoutError as NatsTimeout

            raise NatsTimeout

        async def add_stream(self, config: Any) -> None:  # noqa: ANN401
            from nats.js.errors import BadRequestError

            raise BadRequestError(description="subjects overlap")

    async def connect(**options: Any) -> Connection:  # noqa: ANN401
        seen.update(options)
        return Connection()

    broker = NatsBroker(
        BrokerConfig(URL="nats+tls://u:p@nats.internal:4222", PREFIX="t"),
        connect=connect,
    )
    with pytest.raises(BrokerUnavailable, match="did not answer"):
        await broker.check()
    with pytest.raises(BrokerUnavailable, match="confirm in time"):
        await broker.publish("q", b"x", message_id="1")
    with pytest.raises(BrokerRefused, match="refused the stream"):
        await broker.declare(["q"])
    await broker.close()

    assert seen["servers"] == ["tls://u:p@nats.internal:4222"]
    assert seen["tls"] is not None
    assert seen["closed"]

    async def refuse(**options: Any) -> Connection:  # noqa: ANN401
        raise RuntimeError("auth")

    with pytest.raises(BrokerUnavailable, match="RuntimeError"):
        await NatsBroker(config("nats://h:4222"), connect=refuse).check()


class FakeKafka:
    """aiokafka's producer, consumer, and admin client, over lists in memory."""

    def __init__(self) -> None:
        """No topics; nothing committed."""
        self.topics: dict[str, list[tuple[bytes, list[tuple[str, bytes]]]]] = {}
        self.committed: dict[Any, int] = {}
        self.fail_start = False
        self.fail_commit = False
        self.options: dict[str, Any] = {}
        fake = self

        class Admin:
            def __init__(self, **options: Any) -> None:  # noqa: ANN401
                fake.options = options

            async def start(self) -> None:
                pass

            async def create_topics(self, topics: list[Any]) -> None:
                from aiokafka.errors import TopicAlreadyExistsError

                for topic in topics:
                    if topic.name in fake.topics:
                        raise TopicAlreadyExistsError
                    fake.topics[topic.name] = []

            async def close(self) -> None:
                pass

        class Producer:
            def __init__(self, **options: Any) -> None:  # noqa: ANN401
                assert options["acks"] == "all"

            async def start(self) -> None:
                if fake.fail_start:
                    raise ConnectionError

            async def stop(self) -> None:
                pass

            async def send_and_wait(
                self,
                topic: str,
                value: bytes,
                headers: Any,  # noqa: ANN401
            ) -> None:
                if topic not in fake.topics:
                    raise KeyError(topic)
                fake.topics[topic].append((value, headers))

        class Record:
            def __init__(
                self,
                topic: str,
                offset: int,
                value: bytes,
                headers: Any,  # noqa: ANN401
            ) -> None:
                self.topic, self.offset, self.value, self.headers = (
                    topic,
                    offset,
                    value,
                    headers,
                )

        class Consumer:
            def __init__(self, *topics: str, **options: Any) -> None:  # noqa: ANN401
                self.topics = topics
                self.read = {t: fake.committed.get(t, 0) for t in topics}

            async def start(self) -> None:
                if fake.fail_start:
                    raise ConnectionError

            async def stop(self) -> None:
                pass

            async def getmany(
                self, timeout_ms: int, max_records: int
            ) -> dict[Any, Any]:
                batches: dict[Any, Any] = {}
                left = max_records
                for topic in self.topics:
                    records = fake.topics[topic][self.read[topic] :][:left]
                    if records:
                        start = self.read[topic]
                        batches[topic] = [
                            Record(topic, start + n, value, headers)
                            for n, (value, headers) in enumerate(records)
                        ]
                        self.read[topic] += len(records)
                        left -= len(records)
                if not batches:
                    await asyncio.sleep(0.01)
                return batches

            async def commit(self, offsets: dict[Any, int]) -> None:
                if fake.fail_commit:
                    raise RuntimeError("rebalance")
                fake.committed.update(offsets)

        self.AIOKafkaAdminClient = Admin
        self.AIOKafkaProducer = Producer
        self.AIOKafkaConsumer = Consumer


async def test_kafka_holds_to_the_contract() -> None:
    """Topics made once; a requeued message published again; commits contiguous."""
    fake = FakeKafka()
    broker = KafkaBroker(config("kafka://k1:9092,k2:9092"), kafka=fake)

    assert await exercise(broker) == EXPECTED
    assert fake.options["bootstrap_servers"] == ["k1:9092", "k2:9092"]
    assert fake.committed == {"t.q": 3, "t.r": 3}  # m1 again at the end of t.r.
    await broker.check()
    await broker.close()


async def test_kafka_commits_only_what_every_earlier_message_has() -> None:
    """Answered out of order: the position waits for the first."""
    fake = FakeKafka()
    broker = KafkaBroker(config("kafka://k:9092"), kafka=fake)
    await broker.declare(["q"])
    for number in range(3):
        await broker.publish("q", b"x", message_id=str(number))
    puller = await broker.puller(["q"])
    first, second, third = await puller.fetch(3)

    await puller.ack(third)
    await puller.ack(second)
    assert fake.committed == {}
    await puller.ack(first)
    assert fake.committed == {"t.q": 3}
    fake.fail_commit = True
    await broker.publish("q", b"y", message_id="4")
    (fourth,) = await puller.fetch(1)
    await puller.ack(fourth)  # A refused commit is logged: delivered again later.
    await puller.close()


async def test_kafka_failures_are_named() -> None:
    """No cluster, no topic, no answer in time; TLS and SASL options."""
    fake = FakeKafka()
    fake.fail_start = True
    broker = KafkaBroker(config("kafka://k:9092"), kafka=fake)
    with pytest.raises(BrokerUnavailable, match="ConnectionError"):
        await broker.check()
    with pytest.raises(BrokerUnavailable):
        await broker.puller(["q"])
    fake.fail_start = False
    with pytest.raises(BrokerRefused, match="KeyError"):
        await broker.publish("missing", b"x", message_id="1")

    secure = KafkaBroker(
        BrokerConfig(URL="kafka+ssl://app:s%40cret@k:9093", PREFIX="t"), kafka=fake
    )
    options = secure.options()
    assert (options["security_protocol"], options["sasl_plain_password"]) == (
        "SASL_SSL",
        "s@cret",
    )
    plain = KafkaBroker(BrokerConfig(URL="kafka+ssl://k:9093", PREFIX="t"), kafka=fake)
    assert plain.options()["security_protocol"] == "SSL"
    sasl = KafkaBroker(BrokerConfig(URL="kafka://a:b@k:9092", PREFIX="t"), kafka=fake)
    assert sasl.options()["security_protocol"] == "SASL_PLAINTEXT"


class FakeSqs:
    """SQS's JSON API, over queues in memory."""

    def __init__(self) -> None:
        """No queues."""
        self.queues: dict[str, list[dict[str, Any]]] = {}
        self.hidden: dict[str, tuple[str, dict[str, Any]]] = {}
        self.attributes: dict[str, Any] = {}
        self.calls: list[str] = []
        self.down = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one action."""
        if self.down:
            return httpx2.Response(
                503, json={"__type": "com.amazonaws.sqs#ServiceUnavailable"}
            )
        assert "/us-east-1/sqs/aws4_request" in request.headers["authorization"]
        action = request.headers["x-amz-target"].removeprefix("AmazonSQS.")
        self.calls.append(action)
        body = json.loads(request.content)
        if action == "CreateQueue":
            url = f"https://sqs.example/{body['QueueName']}"
            self.queues.setdefault(url, [])
            self.attributes = body["Attributes"]
            return httpx2.Response(200, json={"QueueUrl": url})
        if action == "ListQueues":
            return httpx2.Response(200, json={"QueueUrls": list(self.queues)})
        url = body["QueueUrl"]
        if url not in self.queues:
            return httpx2.Response(
                400, json={"__type": "com.amazonaws.sqs#QueueDoesNotExist"}
            )
        if action == "SendMessage":
            self.queues[url].append(
                {
                    "Body": body["MessageBody"],
                    "MessageAttributes": body["MessageAttributes"],
                }
            )
            return httpx2.Response(200, json={"MessageId": "x"})
        if action == "ReceiveMessage":
            taken = self.queues[url][: body["MaxNumberOfMessages"]]
            del self.queues[url][: len(taken)]
            messages = []
            for message in taken:
                receipt = f"r{len(self.hidden)}"
                self.hidden[receipt] = (url, message)
                messages.append({**message, "ReceiptHandle": receipt})
            return httpx2.Response(200, json={"Messages": messages} if messages else {})
        receipt = body["ReceiptHandle"]
        if action == "DeleteMessage":
            self.hidden.pop(receipt)
            return httpx2.Response(200, content=b"")
        assert action == "ChangeMessageVisibility"
        assert body["VisibilityTimeout"] == 0
        url, held = self.hidden.pop(receipt)
        self.queues[url].append(held)
        return httpx2.Response(200, content=b"")


@pytest.fixture
def sqs(monkeypatch: pytest.MonkeyPatch) -> FakeSqs:
    """SQS, answering every call of the test, whatever context it runs in."""
    fake = FakeSqs()
    monkeypatch.setattr(
        client_module,
        "_stub",
        ContextVar("stubbed", default=httpx2.MockTransport(fake)),  # noqa: B039
    )
    return fake


async def test_sqs_holds_to_the_contract(sqs: FakeSqs) -> None:
    """Queues made once each; bodies in base64; delete and visibility 0."""
    broker = SqsBroker(config("sqs://AKID:secret@us-east-1", REDELIVER_AFTER=60))

    assert await exercise(broker) == EXPECTED
    assert sqs.calls.count("CreateQueue") == 2
    assert sqs.attributes["VisibilityTimeout"] == "60"
    assert all(not q for q in sqs.queues.values())
    await broker.check()


async def test_sqs_refusals_and_outages(sqs: FakeSqs) -> None:
    """A queue SQS does not know is refused; a 503 or garbage is unavailable."""
    broker = SqsBroker(
        config("sqs://AKID:secret@us-east-1?endpoint=https://sqs.example/")
    )
    broker.urls["gone"] = "https://sqs.example/gone"
    with pytest.raises(BrokerRefused, match="QueueDoesNotExist"):
        await broker.publish("gone", b"x", message_id="1")
    sqs.down = True
    with pytest.raises(BrokerUnavailable, match="ServiceUnavailable"):
        await broker.check()
    with (
        stub(lambda request: httpx2.Response(200, text="<xml/>")),
        pytest.raises(BrokerUnavailable, match="without JSON"),
    ):
        await broker.check()
    with (
        stub(lambda request: httpx2.Response(200, json={})),
        pytest.raises(BrokerUnavailable, match="without a URL"),
    ):
        await broker.declare(["new"])

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no")

    with stub(down), pytest.raises(BrokerUnavailable, match="could not be reached"):
        await broker.check()


async def test_sqs_reads_a_body_that_is_not_base64() -> None:
    """A message another producer sent as text is read as its bytes."""
    from domainkit.broker.sqs import _SqsPuller

    message = _SqsPuller(SqsBroker(config("sqs://us-east-1")), [])._fetched(
        "q", "u", {"Body": "plain text!", "ReceiptHandle": "r"}
    )
    assert (message.body, message.message_id) == (b"plain text!", "")


class Flaky(Puller):
    """Fails ``failures`` times, then hands out one message, then nothing."""

    def __init__(self, failures: int) -> None:
        """Fail first."""
        self.failures = failures
        self.handed = False
        self.closed = False

    async def fetch(self, limit: int) -> Sequence[Fetched]:
        """Fail, then one message."""
        if self.failures:
            self.failures -= 1
            raise BrokerUnavailable("down")
        if self.handed:
            await asyncio.sleep(0.01)
            return []
        self.handed = True
        return [Fetched("q", b"x", "1", None)]

    async def ack(self, message: Fetched) -> None:
        """Nothing."""

    async def requeue(self, message: Fetched) -> None:
        """Nothing."""

    async def close(self) -> None:
        """Closed."""
        self.closed = True


class FlakyBroker(PullBroker):
    """A broker whose puller fails at first."""

    def __init__(self, puller: Flaky) -> None:
        """Hand out ``puller``."""
        super().__init__()
        self._puller = puller

    async def declare(self, queues: Any) -> None:  # noqa: ANN401
        """Nothing."""

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Nothing."""

    async def puller(self, queues: Sequence[str]) -> Puller:
        """The flaky puller."""
        return self._puller


async def test_a_pump_waits_out_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetches that fail are logged and tried again; the message then comes."""
    from domainkit.broker import pull

    monkeypatch.setattr(pull, "FIRST_PAUSE", 0.01)
    flaky = Flaky(failures=2)
    broker = FlakyBroker(flaky)
    got = asyncio.Event()

    async def handle(delivery: Delivery) -> None:
        await delivery.ack()
        got.set()

    subscription = await broker.consume(["q"], 1, handle)
    await asyncio.wait_for(got.wait(), 5)
    await subscription.cancel()
    await broker.close()

    assert flaky.closed


def test_each_scheme_names_its_adapter() -> None:
    """redis, nats, kafka, sqs: their classes; amqp: RabbitMQ's."""
    for url, kind in (
        ("redis://h/0", RedisStreamsBroker),
        ("rediss://:pw@h/0", RedisStreamsBroker),
        ("nats://h:4222", NatsBroker),
        ("kafka://h:9092", KafkaBroker),
        ("sqs://eu-west-1", SqsBroker),
    ):
        assert isinstance(connect_broker(config(url)), kind)
    assert type(connect_broker(config("amqp://u:p@h/"))).__name__ == "RabbitMQBroker"


@pytest.mark.parametrize(
    ("url", "credentials"),
    [
        ("redis://:pw@h/0", True),
        ("redis://h/0", False),
        ("nats://u:p@h:4222", True),
        ("sqs://eu-west-1", True),
        ("kafka://h:9092", False),
    ],
)
def test_credentials_are_read_per_scheme(url: str, credentials: bool) -> None:
    """A password, a user, or SQS's signatures count as credentials."""
    assert config(url).has_credentials() is credentials
