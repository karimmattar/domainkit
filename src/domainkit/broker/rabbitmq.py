"""RabbitMQ (``amqp://``, ``amqps://``), through aio-pika's robust connections.

Topology, all durable: one direct exchange, ``<PREFIX>.work``, and a quorum
queue ``<PREFIX>.<queue>`` per queue, bound by its name. Messages are
persistent and ``mandatory``, and publishing waits for the broker's
confirmation, so a message is either safely queued or the publisher knows
it is not. Consumers acknowledge each message after its work is recorded.
"""

import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from functools import partial
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
    """A verifying TLS context with the configured CA and client certificate."""
    if config.TLS_CA is None and config.TLS_CERT is None:
        return None  # aiormq's default context verifies against the system CAs.
    context = ssl.create_default_context(
        cafile=None if config.TLS_CA is None else str(config.TLS_CA)
    )
    if config.TLS_CERT is not None and config.TLS_KEY is not None:
        context.load_cert_chain(str(config.TLS_CERT), str(config.TLS_KEY))
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

    def __init__(self, consumers: list[tuple[AbstractQueue, str]]) -> None:
        """Own the ``consumers`` (queue, tag)."""
        self._consumers = consumers

    async def cancel(self) -> None:
        """Cancel the consumers; unacknowledged messages stay with this channel."""
        async with _translated():
            for queue, tag in self._consumers:
                await queue.cancel(tag)


class RabbitMQBroker(Broker):
    """Work queues on RabbitMQ, for one ``BROKERS`` entry."""

    def __init__(
        self, config: BrokerConfig, connect: Connect = aio_pika.connect_robust
    ) -> None:
        """Connect as ``config`` says, lazily (``connect`` is for tests)."""
        self.config = config
        self.prefix = config.PREFIX or ""
        self._connect = connect
        self._connection: AbstractConnection | None = None
        self._publishing: tuple[AbstractChannel, AbstractExchange] | None = None

    @property
    def exchange_name(self) -> str:
        """The direct exchange every queue is bound to."""
        return f"{self.prefix}.work"

    def queue_name(self, queue: str) -> str:
        """The RabbitMQ name of the application's ``queue``."""
        return f"{self.prefix}.{queue}"

    async def _open(self) -> AbstractConnection:
        """The connection, opened on first use."""
        if self._connection is None:
            async with _translated():
                self._connection = await self._connect(
                    self.config.URL.get_secret_value(),
                    ssl_context=ssl_context(self.config),
                    timeout=self.config.CONNECT_TIMEOUT,
                    client_properties={"connection_name": f"domainkit {self.prefix}"},
                )
        return self._connection

    async def _declare_on(
        self, channel: AbstractChannel, queues: Iterable[str]
    ) -> tuple[AbstractExchange, list[tuple[str, AbstractQueue]]]:
        """Declare the exchange and ``queues`` on ``channel``, bound by name."""
        exchange = await channel.declare_exchange(
            self.exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )
        declared = []
        for queue in queues:
            amqp_queue = await channel.declare_queue(
                self.queue_name(queue), durable=True, arguments=dict(QUEUE_ARGUMENTS)
            )
            await amqp_queue.bind(exchange, routing_key=queue)
            declared.append((queue, amqp_queue))
        return exchange, declared

    async def _publisher(self) -> tuple[AbstractChannel, AbstractExchange]:
        """The publishing channel (confirms on, returns raise) and the exchange."""
        if self._publishing is None:
            connection = await self._open()
            async with _translated():
                channel = await connection.channel(
                    publisher_confirms=True, on_return_raises=True
                )
                exchange, _ = await self._declare_on(channel, ())
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
        """Consume ``queues`` on a channel of their own, ``prefetch`` at a time."""
        connection = await self._open()
        async with _translated():
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=prefetch)
            _, declared = await self._declare_on(channel, queues)
            consumers = []
            for queue, amqp_queue in declared:
                callback = partial(self._deliver, queue, handler)
                consumers.append((amqp_queue, await amqp_queue.consume(callback)))
        return _RabbitMQSubscription(consumers)

    @staticmethod
    async def _deliver(
        queue: str, handler: Handler, message: AbstractIncomingMessage
    ) -> Any:  # noqa: ANN401 - aio-pika's callback type.
        """Hand one message to ``handler``."""
        await handler(
            Delivery(
                queue,
                message.body,
                message.message_id or "",
                message.ack,
                partial(message.reject, requeue=True),
            )
        )

    async def check(self) -> None:
        """Connect and declare the exchange."""
        await self._publisher()

    async def close(self) -> None:
        """Close the connection and its channels."""
        if self._connection is not None:
            connection, self._connection, self._publishing = (
                self._connection,
                None,
                None,
            )
            async with _translated():
                await connection.close()
