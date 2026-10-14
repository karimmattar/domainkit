"""A small in-memory RabbitMQ behind aio-pika's API, for tests without a server.

It covers what ``RabbitMQBroker`` uses: robust connections, channels with
publisher confirms, durable direct exchanges, queues bound by routing key,
mandatory publishing (unroutable messages raise ``PublishError`` once
``on_return_raises`` is on), prefetch, consumers, acknowledgements, and
requeues. Tests break it on purpose with ``down``, ``refuse``, and
``timeout``. ``test_rabbitmq.py`` runs the same tests on real servers.
"""

import asyncio
import itertools
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

from aio_pika import DeliveryMode, Message
from aio_pika.exceptions import (
    ChannelClosed,
    ChannelPreconditionFailed,
    DeliveryError,
    PublishError,
)
from pamqp.commands import Basic

type Callback = Callable[[Any], Awaitable[Any]]


@dataclass
class FakeIncomingMessage:
    """A delivered message; acknowledging or rejecting settles it."""

    body: bytes
    message_id: str | None
    delivery_mode: DeliveryMode
    _queue: "FakeQueue"
    _channel: "FakeChannel"
    settled: bool = False

    async def ack(self) -> None:
        """Forget the message."""
        self._settle()

    async def reject(self, requeue: bool = False) -> None:
        """Put the message back at the head of its queue, or drop it."""
        self._settle()
        if requeue:
            self._queue.messages.appendleft(self)
            self._queue.wake()

    def _settle(self) -> None:
        if self.settled:
            raise RuntimeError("A message is settled once.")
        self.settled = True
        self._channel.unacked -= 1
        # The prefetch is the channel's: any of its consumers may have room now.
        for queue in self._queue.server.queues.values():
            queue.wake()


@dataclass
class FakeQueue:
    """A queue: its messages, arguments, and consumers."""

    name: str
    arguments: dict[str, Any]
    server: "FakeAMQP"
    messages: deque[FakeIncomingMessage] = field(default_factory=deque)
    consumers: dict[str, tuple["FakeChannel", Callback]] = field(default_factory=dict)
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _pumps: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    async def bind(self, exchange: "FakeExchange", routing_key: str) -> None:
        """Route the exchange's messages with ``routing_key`` here."""
        self.server.bindings.setdefault((exchange.name, routing_key), set()).add(
            self.name
        )

    async def consume(self, callback: Callback, no_ack: bool = False) -> str:
        """Hand messages to ``callback`` within the channel's prefetch."""
        channel = self.server.current_channel(self)
        tag = f"ctag-{next(self.server.tags)}"
        self.consumers[tag] = (channel, callback)
        self._pumps[tag] = asyncio.create_task(self._pump(tag, channel, callback))
        return tag

    async def cancel(self, tag: str) -> None:
        """Stop handing messages to the consumer ``tag``."""
        self.consumers.pop(tag)
        pump = self._pumps.pop(tag)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    def wake(self) -> None:
        """Let consumers look again."""
        self._ready.set()

    async def _pump(self, tag: str, channel: "FakeChannel", callback: Callback) -> None:
        while True:
            while not self.messages or not channel.has_room():
                self._ready.clear()
                await self._ready.wait()
            message = self.messages.popleft()
            message.settled = False
            message._channel = channel
            channel.unacked += 1
            self.server.spawn(callback(message))


@dataclass
class FakeExchange:
    """A durable direct exchange."""

    name: str
    channel: "FakeChannel"

    async def publish(
        self,
        message: Message,
        routing_key: str,
        *,
        mandatory: bool,
        timeout: float,  # noqa: ASYNC109 - aio-pika's signature.
    ) -> object:
        """Route ``message`` to the bound queues, as a confirmed publication."""
        server = self.channel.server
        if server.down:
            raise ChannelClosed(None, None)
        if server.timeout:
            raise TimeoutError
        if server.refuse:
            raise DeliveryError(None, Basic.Nack(delivery_tag=1))
        server.published.append((routing_key, message, mandatory, timeout))
        names = server.bindings.get((self.name, routing_key), set())
        if not names and mandatory and self.channel.on_return_raises:
            returned = Basic.Return(312, "NO_ROUTE", self.name, routing_key)
            raise PublishError(cast(Any, SimpleNamespace(delivery=returned)), returned)
        for name in names:
            queue = server.queues[name]
            queue.messages.append(
                FakeIncomingMessage(
                    message.body,
                    message.message_id,
                    message.delivery_mode,
                    queue,
                    self.channel,
                )
            )
            queue.wake()
        return object()


@dataclass
class FakeChannel:
    """A channel: confirms, returns, prefetch, and unacknowledged messages."""

    server: "FakeAMQP"
    publisher_confirms: bool
    on_return_raises: bool
    prefetch: int = 0
    unacked: int = 0

    async def set_qos(self, prefetch_count: int) -> None:
        """Limit unacknowledged messages on this channel."""
        self.prefetch = prefetch_count

    def has_room(self) -> bool:
        """Report whether the prefetch allows another delivery."""
        return self.prefetch == 0 or self.unacked < self.prefetch

    async def declare_exchange(
        self, name: str, type: object, durable: bool
    ) -> FakeExchange:
        """Return the exchange, declaring it once."""
        self.server.exchanges[name] = durable
        return FakeExchange(name, self)

    async def declare_queue(
        self, name: str, durable: bool, arguments: dict[str, Any]
    ) -> FakeQueue:
        """Return the queue; changing an existing one's arguments fails."""
        queue = self.server.queues.get(name)
        if queue is None:
            queue = self.server.queues[name] = FakeQueue(name, arguments, self.server)
        elif queue.arguments != arguments:
            raise ChannelPreconditionFailed(406, "PRECONDITION_FAILED")
        self.server.last_channel = self
        return queue


class FakeConnection:
    """A robust connection to the fake server."""

    def __init__(self, server: "FakeAMQP") -> None:
        """Open on ``server``."""
        self.server = server
        self.is_closed = False

    async def channel(
        self, publisher_confirms: bool = True, on_return_raises: bool = False
    ) -> FakeChannel:
        """Open a channel."""
        channel = FakeChannel(self.server, publisher_confirms, on_return_raises)
        self.server.last_channel = channel
        return channel

    async def close(self) -> None:
        """Close the connection."""
        self.is_closed = True


class FakeAMQP:
    """The server: exchanges, queues, bindings, and what was published."""

    def __init__(self) -> None:
        """Start empty and up."""
        self.exchanges: dict[str, bool] = {}
        self.queues: dict[str, FakeQueue] = {}
        self.bindings: dict[tuple[str, str], set[str]] = {}
        self.published: list[tuple[str, Message, bool, float]] = []
        self.connections: list[tuple[str, dict[str, Any]]] = []
        self.tags = itertools.count(1)
        self.last_channel: FakeChannel | None = None
        self.down = False
        self.refuse = False
        self.timeout = False
        self._tasks: set[asyncio.Task[Any]] = set()

    async def connect(self, url: str, **options: Any) -> FakeConnection:  # noqa: ANN401
        """Open a connection, or fail as an unreachable server does."""
        if self.down:
            raise ConnectionError("Connection refused")
        self.connections.append((url, options))
        return FakeConnection(self)

    def current_channel(self, queue: FakeQueue) -> FakeChannel:
        """The channel that last declared ``queue`` (consumers use it)."""
        assert self.last_channel is not None
        return self.last_channel

    def spawn(self, coroutine: Awaitable[Any]) -> None:
        """Run a consumer callback as a task, as aio-pika does."""
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
