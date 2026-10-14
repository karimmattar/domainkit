"""The broker port: work queues that hand each message to one consumer.

A message published to a queue goes to one of the queue's consumers, which
acknowledges it once its work is recorded; a message whose consumer goes
away before that goes to another. Adapters translate their failures into
``BrokerUnavailable`` (unreachable, or no confirmation in time) and
``BrokerRefused`` (the broker said no, as for a message no queue takes).
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass


class BrokerError(Exception):
    """Base class of broker errors."""


class BrokerUnavailable(BrokerError):
    """The broker could not be reached, or did not confirm in time."""


class BrokerRefused(BrokerError):
    """The broker refused a message (no queue took it, or it said no)."""


@dataclass(frozen=True, slots=True)
class Delivery:
    """One message handed to a consumer; ``ack`` or ``requeue`` it exactly once."""

    queue: str
    body: bytes
    message_id: str
    ack: Callable[[], Awaitable[None]]
    """Done with it: the broker forgets the message."""
    requeue: Callable[[], Awaitable[None]]
    """Not now: the broker hands the message out again."""


type Handler = Callable[[Delivery], Awaitable[None]]


class Subscription(ABC):
    """Deliveries flowing from queues to a handler."""

    @abstractmethod
    async def cancel(self) -> None:
        """Stop new deliveries; deliveries being handled carry on."""


class Broker(ABC):
    """Durable work queues. Queue names are the application's; adapters prefix them."""

    @abstractmethod
    async def declare(self, queues: Iterable[str]) -> None:
        """Create the queues if they do not exist yet."""

    @abstractmethod
    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Hand ``body`` to ``queue``; return once the broker has it safely."""

    @abstractmethod
    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Call ``handler`` for messages of ``queues``, at most ``prefetch`` at once."""

    async def check(self) -> None:  # noqa: B027 - optional; most brokers have it.
        """Raise if the broker cannot be reached."""

    async def close(self) -> None:  # noqa: B027 - optional, for connections.
        """Release connections; the broker is not used afterwards."""
