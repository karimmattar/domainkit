"""Apache Kafka (``kafka://``, ``kafka+ssl://``) as a work queue (the ``kafka`` extra).

Each queue is a topic, ``<PREFIX>.<queue>``, of ``PARTITIONS``
partitions, read by one consumer group, ``<PREFIX>.workers``: each
partition goes to one worker, so the partitions bound how many share a
queue. Publishing waits for every in-sync replica (``acks=all``, an
idempotent producer). Kafka keeps a position per partition, not an
answer per message: a message's position is committed once it, and
every message before it in its partition, is answered, so a worker that
goes away hands its unanswered messages, and those after them, to
another (at least once, as the outbox expects). A requeued message is
published again at the end of its topic, then answered.

``kafka://host1:9092,host2:9092`` lists the bootstrap servers; ``kafka+ssl://``
speaks TLS, with ``TLS_CA`` and a client certificate if given. A user
and password in the URL sign in with SASL (SCRAM-SHA-512), over TLS only.
"""

import asyncio
import contextlib
from collections.abc import Iterable, Sequence
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from domainkit.broker.base import BrokerRefused, BrokerUnavailable
from domainkit.broker.pull import Fetched, PullBroker, Puller
from domainkit.conf import BrokerConfig

__all__ = ["KafkaBroker"]

WAIT_MS: Final = 1000
MESSAGE_ID: Final = "message_id"


class KafkaBroker(PullBroker):
    """Work queues on Kafka topics (see the module)."""

    def __init__(self, config: BrokerConfig, *, kafka: Any = None) -> None:  # noqa: ANN401
        """Connect lazily; ``kafka`` replaces the ``aiokafka`` module (tests)."""
        super().__init__()
        self.config = config
        self.publish_timeout = config.PUBLISH_TIMEOUT
        self.group = f"{config.PREFIX}.workers"
        self._kafka = kafka
        self._producer: Any = None
        self._lock = asyncio.Lock()

    @property
    def kafka(self) -> Any:  # noqa: ANN401 - the aiokafka module.
        """``aiokafka``, or its stand-in."""
        if self._kafka is None:
            import aiokafka

            self._kafka = aiokafka
        return self._kafka

    def topic(self, queue: str) -> str:
        """The topic of ``queue``."""
        return f"{self.config.PREFIX}.{queue}"

    def options(self) -> dict[str, Any]:
        """What every client of the cluster is given: servers, TLS, SASL."""
        config = self.config
        parts = urlsplit(config.URL.get_secret_value())
        servers = parts.netloc.rpartition("@")[2]
        options: dict[str, Any] = {
            "bootstrap_servers": servers.split(","),
            "request_timeout_ms": int(config.PUBLISH_TIMEOUT * 1000),
            "connections_max_idle_ms": 540_000,
        }
        tls = config.scheme == "kafka+ssl"
        if tls:
            from aiokafka.helpers import create_ssl_context

            options["ssl_context"] = create_ssl_context(
                cafile=None if config.TLS_CA is None else str(config.TLS_CA),
                certfile=None if config.TLS_CERT is None else str(config.TLS_CERT),
                keyfile=None if config.TLS_KEY is None else str(config.TLS_KEY),
            )
        if parts.username:
            options.update(
                security_protocol="SASL_SSL" if tls else "SASL_PLAINTEXT",
                sasl_mechanism="SCRAM-SHA-512",
                sasl_plain_username=unquote(parts.username),
                sasl_plain_password=unquote(parts.password or ""),
            )
        elif tls:
            options["security_protocol"] = "SSL"
        return options

    async def producer(self) -> Any:  # noqa: ANN401 - aiokafka's producer.
        """The producer, started on first use."""
        async with self._lock:
            if self._producer is None:
                producer = self.kafka.AIOKafkaProducer(
                    acks="all", enable_idempotence=True, **self.options()
                )
                try:
                    async with asyncio.timeout(self.config.CONNECT_TIMEOUT):
                        await producer.start()
                except Exception as exc:
                    await producer.stop()
                    raise BrokerUnavailable(
                        f"Kafka could not be reached ({type(exc).__name__})."
                    ) from exc
                self._producer = producer
            return self._producer

    async def declare(self, queues: Iterable[str]) -> None:
        """Create the missing topics, of ``PARTITIONS`` partitions each."""
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic
        from aiokafka.errors import TopicAlreadyExistsError

        admin_class = getattr(self.kafka, "AIOKafkaAdminClient", AIOKafkaAdminClient)
        admin = admin_class(**self.options())
        topics = [
            NewTopic(self.topic(queue), self.config.PARTITIONS, self.config.REPLICAS)
            for queue in queues
        ]
        try:
            async with asyncio.timeout(self.config.CONNECT_TIMEOUT):
                await admin.start()
                with contextlib.suppress(TopicAlreadyExistsError):
                    await admin.create_topics(topics)
        except TimeoutError as exc:
            raise BrokerUnavailable("Kafka did not answer in time (declare).") from exc
        finally:
            await admin.close()

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Send, and wait for every in-sync replica to have it."""
        producer = await self.producer()
        try:
            async with asyncio.timeout(self.publish_timeout):
                await producer.send_and_wait(
                    self.topic(queue),
                    body,
                    headers=[(MESSAGE_ID, message_id.encode())],
                )
        except TimeoutError as exc:
            raise BrokerUnavailable("Kafka did not confirm in time (publish).") from exc
        except Exception as exc:
            raise BrokerRefused(
                f"Kafka refused the message ({type(exc).__name__})."
            ) from exc

    async def puller(self, queues: Sequence[str]) -> Puller:
        """A consumer of the group, subscribed to ``queues``' topics."""
        consumer = self.kafka.AIOKafkaConsumer(
            *[self.topic(queue) for queue in queues],
            group_id=self.group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            **self.options(),
        )
        try:
            async with asyncio.timeout(self.config.CONNECT_TIMEOUT):
                await consumer.start()
        except Exception as exc:
            await consumer.stop()
            raise BrokerUnavailable(
                f"Kafka could not be reached ({type(exc).__name__})."
            ) from exc
        return _KafkaPuller(self, consumer, {self.topic(q): q for q in queues})

    async def check(self) -> None:
        """The producer starts: the cluster answers."""
        await self.producer()

    async def close(self) -> None:
        """Wait for answers, then stop the producer."""
        await super().close()
        if self._producer is not None:
            producer, self._producer = self._producer, None
            await producer.stop()


class _KafkaPuller(Puller):
    """A group consumer, committing each partition up to its answered messages."""

    def __init__(
        self,
        broker: KafkaBroker,
        consumer: Any,  # noqa: ANN401 - aiokafka's consumer.
        queues: dict[str, str],
    ) -> None:
        """Read with ``consumer``; ``queues`` names each topic's queue."""
        self.broker = broker
        self.consumer = consumer
        self.queues = queues
        self.answered: dict[Any, set[int]] = {}
        """Per partition: offsets answered beyond the committed position."""
        self.position: dict[Any, int] = {}
        """Per partition: the next offset to commit up to."""
        self._lock = asyncio.Lock()

    async def fetch(self, limit: int) -> Sequence[Fetched]:
        """Up to ``limit`` records of any of the topics, waiting a second."""
        batches = await self.consumer.getmany(timeout_ms=WAIT_MS, max_records=limit)
        found: list[Fetched] = []
        for partition, records in batches.items():
            self.position.setdefault(partition, records[0].offset if records else 0)
            for record in records:
                headers = dict(record.headers or ())
                found.append(
                    Fetched(
                        self.queues[record.topic],
                        bytes(record.value or b""),
                        bytes(headers.get(MESSAGE_ID, b"")).decode(),
                        (partition, record.offset),
                    )
                )
        return found

    async def ack(self, message: Fetched) -> None:
        """Answered: commit its partition as far as every message is."""
        partition, offset = message.handle
        async with self._lock:
            answered = self.answered.setdefault(partition, set())
            answered.add(offset)
            start = position = self.position.get(partition, offset)
            while position in answered:
                answered.discard(position)
                position += 1
            if position == start:
                return
            self.position[partition] = position
            try:
                await self.consumer.commit({partition: position})
            except Exception as exc:  # noqa: BLE001 - a rebalance: delivered again.
                from domainkit.broker.pull import logger

                logger.warning(
                    "Kafka refused a commit (%s): its messages come again.",
                    type(exc).__name__,
                )

    async def requeue(self, message: Fetched) -> None:
        """Published again at the end of its topic, then answered."""
        await self.broker.publish(
            message.queue, message.body, message_id=message.message_id
        )
        await self.ack(message)

    async def close(self) -> None:
        """Leave the group."""
        await self.consumer.stop()
