"""RabbitMQ (``amqp://``, ``amqps://``), through aio-pika's robust connections.

Topology, all durable: one direct exchange, ``<PREFIX>.work``, and a quorum
queue ``<PREFIX>.<queue>`` per queue, bound by its name. Messages are
persistent and ``mandatory``, and publishing waits for the broker's
confirmation, so a message is either safely queued or the publisher knows
it is not. Consumers acknowledge each message after its work is recorded.

Every call to the broker has a deadline: ``CONNECT_TIMEOUT`` for opening the
connection and channels, declaring, setting the prefetch, and starting or
stopping consumers, and ``PUBLISH_TIMEOUT`` for each confirmation. A broker
that accepts connections but stops answering fails these calls instead of
holding the caller (and the locks callers share) until its heartbeat ends.
"""

import asyncio
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from functools import cache, partial
from pathlib import Path
from typing import Any, Final

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)
from aio_pika.exceptions import (
    CONNECTION_EXCEPTIONS,
    ChannelNotFoundEntity,
    ChannelPreconditionFailed,
    DeliveryError,
    PublishError,
)
from aiormq.exceptions import ChannelAccessRefused

from domainkit.broker.base import (
    Broker,
    BrokerError,
    BrokerRefused,
    BrokerUnavailable,
    Delivery,
    Handler,
    Subscription,
)
from domainkit.conf import BrokerConfig

type Connect = Callable[..., Awaitable[AbstractConnection]]

QUEUE_ARGUMENTS: Final = {"x-queue-type": "quorum"}
"""Replicated, durable queues; change them only with a policy (they are fixed)."""


def ssl_context(config: BrokerConfig) -> ssl.SSLContext | None:
    """A verifying TLS context with the configured CA and client certificate.

    Built once per process for each set of files; the broker builds it on a
    worker thread, since reading the CA store and the files blocks.
    """
    if config.TLS_CA is None and config.TLS_CERT is None:
        return None  # aiormq's default context verifies against the system CAs.
    return _ssl_context(config.TLS_CA, config.TLS_CERT, config.TLS_KEY)


@cache
def _ssl_context(
    ca: Path | None, cert: Path | None, key: Path | None
) -> ssl.SSLContext:
    """Build the context ``ssl_context`` describes (once, then cached)."""
    context = ssl.create_default_context(cafile=None if ca is None else str(ca))
    if cert is not None and key is not None:
        context.load_cert_chain(str(cert), str(key))
    return context


@asynccontextmanager
async def _translated() -> AsyncIterator[None]:
    """Turn aio-pika's errors into broker errors: refusals, or unavailability."""
    try:
        yield
    except (DeliveryError, PublishError) as exc:
        raise BrokerRefused(f"RabbitMQ refused the message: {exc}") from exc
    except (
        ChannelAccessRefused,
        ChannelPreconditionFailed,
        ChannelNotFoundEntity,
    ) as exc:
        raise BrokerRefused(f"RabbitMQ refused: {exc!r}") from exc
    except (*CONNECTION_EXCEPTIONS, TimeoutError) as exc:
        raise BrokerUnavailable(f"RabbitMQ is unavailable: {exc!r}") from exc


class _RabbitMQSubscription(Subscription):
    """Consumers on one channel."""

    def __init__(
        self, consumers: list[tuple[AbstractQueue, str]], timeout: float
    ) -> None:
        """Own the ``consumers`` (queue, tag); cancel each within ``timeout``."""
        self._consumers = consumers
        self._timeout = timeout

    async def cancel(self) -> None:
        """Cancel the consumers; unacknowledged messages stay with this channel.

        Each consumer is cancelled, even after one fails; then the first
        failure is raised.
        """
        first: BaseException | None = None
        for queue, tag in self._consumers:
            try:
                async with _translated():
                    await queue.cancel(tag, timeout=self._timeout)
            except BrokerError as exc:
                first = first or exc
        if first is not None:
            raise first


class RabbitMQBroker(Broker):
    """Work queues on RabbitMQ, for one ``BROKERS`` entry."""

    def __init__(
        self, config: BrokerConfig, connect: Connect = aio_pika.connect_robust
    ) -> None:
        """Connect as ``config`` says, lazily (``connect`` is for tests)."""
        self.config = config
        self.prefix = config.PREFIX or ""
        self.publish_timeout = config.CONNECT_TIMEOUT + config.PUBLISH_TIMEOUT
        self._connect = connect
        self._connection: AbstractConnection | None = None
        self._publishing: tuple[AbstractChannel, AbstractExchange] | None = None
        # Callers racing on first use share one connection and one channel.
        self._opening = asyncio.Lock()
        self._opening_publisher = asyncio.Lock()

    @property
    def exchange_name(self) -> str:
        """The direct exchange every queue is bound to."""
        return f"{self.prefix}.work"

    def queue_name(self, queue: str) -> str:
        """The RabbitMQ name of the application's ``queue``."""
        return f"{self.prefix}.{queue}"

    async def _open(self) -> AbstractConnection:
        """The connection, opened on first use."""
        async with self._opening:
            if self._connection is None:
                async with _translated():
                    context = await asyncio.to_thread(ssl_context, self.config)
                    self._connection = await self._connect(
                        self.config.URL.get_secret_value(),
                        ssl_context=context,
                        timeout=self.config.CONNECT_TIMEOUT,
                        client_properties={
                            "connection_name": f"domainkit {self.prefix}"
                        },
                    )
            return self._connection

    async def _declare_on(
        self, channel: AbstractChannel, queues: Iterable[str]
    ) -> tuple[AbstractExchange, list[tuple[str, AbstractQueue]]]:
        """Declare the exchange and ``queues`` on ``channel``, bound by name."""
        timeout = self.config.CONNECT_TIMEOUT
        exchange = await channel.declare_exchange(
            self.exchange_name,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
            timeout=timeout,
        )
        declared = []
        for queue in queues:
            amqp_queue = await channel.declare_queue(
                self.queue_name(queue),
                durable=True,
                arguments=dict(QUEUE_ARGUMENTS),
                timeout=timeout,
            )
            await amqp_queue.bind(exchange, routing_key=queue, timeout=timeout)
            declared.append((queue, amqp_queue))
        return exchange, declared

    async def _channel(self, **options: bool) -> AbstractChannel:
        """Open a channel on the connection, within ``CONNECT_TIMEOUT``."""
        connection = await self._open()
        async with _translated(), asyncio.timeout(self.config.CONNECT_TIMEOUT):
            return await connection.channel(**options)

    async def _publisher(self) -> tuple[AbstractChannel, AbstractExchange]:
        """The publishing channel (confirms on, returns raise) and the exchange.

        Every wait under the lock has a deadline, so a stalled broker fails
        the callers queued behind it instead of holding them.
        """
        async with self._opening_publisher:
            if self._publishing is None:
                channel = await self._channel(
                    publisher_confirms=True, on_return_raises=True
                )
                try:
                    async with _translated():
                        exchange, _ = await self._declare_on(channel, ())
                except BaseException:
                    await _discard(channel, self.config.CONNECT_TIMEOUT)
                    raise
                self._publishing = (channel, exchange)
            return self._publishing

    async def declare(self, queues: Iterable[str]) -> None:
        """Declare the exchange and the queues, bound by name."""
        channel, _ = await self._publisher()
        async with _translated():
            await self._declare_on(channel, queues)

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Publish a persistent, mandatory message and wait for its confirmation."""
        _, exchange = await self._publisher()
        message = aio_pika.Message(
            body,
            message_id=message_id,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        async with _translated():
            await exchange.publish(
                message,
                routing_key=queue,
                mandatory=True,
                timeout=self.config.PUBLISH_TIMEOUT,
            )

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Consume ``queues`` on a channel of their own, ``prefetch`` per queue.

        The prefetch is each consumer's: quorum queues refuse a prefetch
        shared by a channel (RabbitMQ 4.3 quietly makes it per consumer), so
        a caller that must bound its work across queues counts it itself.
        """
        timeout = self.config.CONNECT_TIMEOUT
        channel = await self._channel()
        try:
            async with _translated():
                await channel.set_qos(prefetch_count=prefetch, timeout=timeout)
                _, declared = await self._declare_on(channel, queues)
                consumers = []
                for queue, amqp_queue in declared:
                    callback = partial(self._deliver, queue, handler)
                    tag = await amqp_queue.consume(callback, timeout=timeout)
                    consumers.append((amqp_queue, tag))
        except BaseException:
            await _discard(channel, timeout)
            raise
        return _RabbitMQSubscription(consumers, timeout)

    @staticmethod
    async def _deliver(
        queue: str, handler: Handler, message: AbstractIncomingMessage
    ) -> Any:  # noqa: ANN401 - aio-pika's callback type.
        """Hand one message to ``handler``; answers it cannot give are broker errors.

        aio-pika raises its own errors from ``ack`` and ``reject`` once the
        delivery's channel closed, or for a message already answered.
        """

        async def ack() -> None:
            async with _translated():
                await message.ack()

        async def requeue() -> None:
            async with _translated():
                await message.reject(requeue=True)

        await handler(
            Delivery(queue, message.body, message.message_id or "", ack, requeue)
        )

    async def check(self) -> None:
        """Connect and declare the exchange."""
        await self._publisher()

    async def close(self) -> None:
        """Close the connection and its channels, within ``CONNECT_TIMEOUT``."""
        if self._connection is not None:
            connection, self._connection, self._publishing = (
                self._connection,
                None,
                None,
            )
            async with _translated(), asyncio.timeout(self.config.CONNECT_TIMEOUT):
                await connection.close()


async def _discard(channel: AbstractChannel, within: float) -> None:
    """Close a channel that failed to set up, within ``within`` seconds.

    A timed-out call can leave its answer in flight, so the channel is not
    reused: the next caller opens a fresh one. The channel may be broken
    already, so closing it never raises.
    """
    with suppress(Exception):
        async with asyncio.timeout(within):
            await channel.close()
