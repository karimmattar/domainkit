"""A small in-memory RabbitMQ behind aio-pika's API, for tests without a server.

It covers what ``RabbitMQBroker`` uses: robust connections, channels with
publisher confirms, durable direct exchanges, queues bound by routing key,
mandatory publishing (unroutable messages raise ``PublishError`` once
``on_return_raises`` is on), prefetch per consumer, consumers,
acknowledgements, and requeues. Tests break it on purpose with ``down``, ``refuse``, and
``timeout``, and stall the calls named in ``stalled`` (a server that
accepts connections but stops answering): a stalled call with aio-pika's
``timeout=`` raises ``TimeoutError`` once it passes, as aio-pika does, and
one without waits until ``release()``. ``test_rabbitmq.py`` runs the same
tests on real servers.
"""

import asyncio
import itertools
from collections import Counter, deque
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
class FakeConsumer:
    """A consumer of one queue, with its own prefetch (RabbitMQ's for quorum queues).

    ``set_qos`` before ``consume`` sets it, whether or not ``global_`` is
    asked for: quorum queues refuse a prefetch shared by a channel, and
    RabbitMQ 4.3 quietly makes it per consumer.
    """

    tag: str
    channel: "FakeChannel"
    callback: Callback
    prefetch: int
    unacked: int = 0

    def has_room(self) -> bool:
        """Report whether the prefetch allows another delivery."""
        return self.prefetch == 0 or self.unacked < self.prefetch


@dataclass
class FakeIncomingMessage:
    """A delivered message; acknowledging or rejecting settles it."""

    body: bytes
    message_id: str | None
    delivery_mode: DeliveryMode
    _queue: "FakeQueue"
    _consumer: FakeConsumer | None = None
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
        if self._consumer is not None:
            self._consumer.unacked -= 1
            self._consumer.channel.unacked -= 1
        self._queue.wake()


@dataclass
class FakeQueue:
    """A queue: its messages, arguments, and consumers."""

    name: str
    arguments: dict[str, Any]
    server: "FakeAMQP"
    messages: deque[FakeIncomingMessage] = field(default_factory=deque)
    consumers: dict[str, FakeConsumer] = field(default_factory=dict)
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _pumps: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    async def bind(
        self,
        exchange: "FakeExchange",
        routing_key: str,
        timeout: float | None = None,  # noqa: ASYNC109 - aio-pika's signature.
    ) -> None:
        """Route the exchange's messages with ``routing_key`` here."""
        await self.server.answer("bind", timeout)
        self.server.bindings.setdefault((exchange.name, routing_key), set()).add(
            self.name
        )

    async def consume(
        self,
        callback: Callback,
        no_ack: bool = False,
        timeout: float | None = None,  # noqa: ASYNC109 - aio-pika's signature.
    ) -> str:
        """Hand messages to ``callback`` within the prefetch."""
        await self.server.answer("consume", timeout)
        channel = self.server.current_channel(self)
        tag = f"ctag-{next(self.server.tags)}"
        consumer = FakeConsumer(tag, channel, callback, channel.prefetch)
        self.consumers[tag] = consumer
        self._pumps[tag] = asyncio.create_task(self._pump(consumer))
        return tag

    async def cancel(
        self,
        tag: str,
        timeout: float | None = None,  # noqa: ASYNC109 - aio-pika's signature.
    ) -> None:
        """Stop handing messages to the consumer ``tag``."""
        if self.server.down:
            raise ChannelClosed(None, None)
        await self.server.answer("cancel", timeout)
        self.consumers.pop(tag)
        pump = self._pumps.pop(tag)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    def wake(self) -> None:
        """Let consumers look again."""
        self._ready.set()

    async def _pump(self, consumer: FakeConsumer) -> None:
        while True:
            while not self.messages or not consumer.has_room():
                self._ready.clear()
                await self._ready.wait()
            message = self.messages.popleft()
            message.settled = False
            message._consumer = consumer
            consumer.unacked += 1
            consumer.channel.unacked += 1
            self.server.spawn(consumer.callback(message))


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
                    message.body, message.message_id, message.delivery_mode, queue
                )
            )
            queue.wake()
        return object()


@dataclass
class FakeChannel:
    """A channel: confirms, returns, the next consumers' prefetch, and unacked."""

    server: "FakeAMQP"
    publisher_confirms: bool
    on_return_raises: bool
    prefetch: int = 0
    unacked: int = 0
    closed: bool = False

    async def set_qos(
        self,
        prefetch_count: int,
        global_: bool = False,
        timeout: float | None = None,  # noqa: ASYNC109 - aio-pika's signature.
    ) -> None:
        """Limit unacknowledged messages on this channel."""
        await self.server.answer("set_qos", timeout)
        self.server.qos.append((prefetch_count, global_))
        self.prefetch = prefetch_count

    async def declare_exchange(
        self,
        name: str,
        type: object,
        durable: bool,
        timeout: float | None = None,  # noqa: ASYNC109 - aio-pika's signature.
    ) -> FakeExchange:
        """Return the exchange, declaring it once."""
        await self.server.answer("declare_exchange", timeout)
        self.server.exchanges[name] = durable
        return FakeExchange(name, self)

    async def declare_queue(
        self,
        name: str,
        durable: bool,
        arguments: dict[str, Any],
        timeout: float | None = None,  # noqa: ASYNC109 - aio-pika's signature.
    ) -> FakeQueue:
        """Return the queue; changing an existing one's arguments fails."""
        await self.server.answer("declare_queue", timeout)
        queue = self.server.queues.get(name)
        if queue is None:
            queue = self.server.queues[name] = FakeQueue(name, arguments, self.server)
        elif queue.arguments != arguments:
            raise ChannelPreconditionFailed(406, "PRECONDITION_FAILED")
        self.server.last_channel = self
        return queue

    async def close(self) -> None:
        """Close the channel."""
        await self.server.answer("close_channel", None)
        self.closed = True


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
        await self.server.answer("channel", None)
        channel = FakeChannel(self.server, publisher_confirms, on_return_raises)
        self.server.last_channel = channel
        return channel

    async def close(self) -> None:
        """Close the connection."""
        await self.server.answer("close", None)
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
        self.stalled: set[str] = set()
        self.timeouts: dict[str, float | None] = {}
        """The ``timeout=`` each kind of call was last made with."""
        self.calls: Counter[str] = Counter()
        """How often each kind of call was made."""
        self.qos: list[tuple[int, bool]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._released = asyncio.Event()

    async def answer(self, call: str, timeout: float | None) -> None:  # noqa: ASYNC109
        """Answer ``call`` at once, or stall it if it is in ``stalled``."""
        self.timeouts[call] = timeout
        self.calls[call] += 1
        if call in self.stalled:
            async with asyncio.timeout(timeout):
                await self._released.wait()

    def release(self) -> None:
        """Let stalled calls answer."""
        self._released.set()

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
