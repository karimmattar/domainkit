"""The circuit breakers of publishing on commit and of the relay (ADRs 0042, 0142).

While a broker keeps failing, publishing each commit's messages only costs:
two transactions on the web process's pool (lock, then unlock) and a log
line per message, for messages the relay sends anyway. The breaker stops
that. After ``BREAKER_FAILURES`` failed broker calls in a row it opens, and
for ``BREAKER_COOLDOWN`` seconds commits leave their messages to the relay
without touching the database or the broker. Then the next round probes:
the broker's ``check`` first, before any row is locked, then the round's
publications. A confirmed publication closes the breaker; a failure opens
it for another cooldown. It logs once when it opens and once when it
closes.

A refusal (``BrokerRefused``: no queue took the message) is an answer, so
it counts as reaching the broker, not as a failure.

Each relaying worker has a breaker of its own: while it is open, the
worker runs the outbox's due messages itself, as a worker without a broker
does (``dispatch.BrokerWorkers``).
"""

import logging
from collections.abc import Awaitable, Iterable
from weakref import ref

from domainkit import clock
from domainkit.broker import (
    Broker,
    BrokerRefused,
    BrokerUnavailable,
    Handler,
    Subscription,
)
from domainkit.broker.base import publish_with_priority

try:
    from .metrics import observe_breaker
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_breaker(broker: str, *, is_open: bool) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)


class Breaker:
    """Counts a broker's failures in a row, and opens after too many."""

    def __init__(
        self,
        broker: str,
        failures: int,
        cooldown: float,
        *,
        meanwhile: str = "commits leave their messages to the relay",
        again: str = "confirms messages again; commits publish them again",
        observed: bool = True,
    ) -> None:
        """Open after ``failures`` in a row, for ``cooldown`` seconds each time.

        ``meanwhile`` and ``again`` say, in its log lines, what happens while
        it is open and once it closes; ``observed`` reports it as
        ``messaging_publisher_breaker_open`` (commits' breaker only).
        """
        self.broker = broker
        self.threshold = failures
        self.cooldown = cooldown
        self.meanwhile = meanwhile
        self.again = again
        self.observed = observed
        self.failures = 0
        """Failed calls in a row."""
        self.opened_at: float | None = None
        """When it last opened (``clock.monotonic``), or None while closed."""
        self._observe(is_open=False)

    def __repr__(self) -> str:
        """Show the broker and the state."""
        return f"<Breaker {self.broker!r} {self.state}>"

    @property
    def state(self) -> str:
        """``closed``, ``open`` (cooling down), or ``probing`` (cooled down)."""
        if self.opened_at is None:
            return "closed"
        if clock.monotonic() - self.opened_at < self.cooldown:
            return "open"
        return "probing"

    def allows(self) -> bool:
        """Report whether publishing may go ahead (closed, or probing)."""
        return self.state != "open"

    def succeeded(self) -> None:
        """Count a confirmed publication: the broker works, so close."""
        if self.opened_at is not None:
            logger.warning("Broker %r %s", self.broker, self.again)
            self._observe(is_open=False)
        self.failures = 0
        self.opened_at = None

    def reached(self) -> None:
        """Count an answer that is not a confirmation (a refusal)."""
        self.failures = 0

    def failed(self, exc: BaseException) -> None:
        """Count a failure; open after too many in a row, or when probing."""
        self.failures += 1
        if self.opened_at is not None:
            self.opened_at = clock.monotonic()  # The probe failed: cool down again.
            return
        if self.failures >= self.threshold:
            self.opened_at = clock.monotonic()
            self._observe(is_open=True)
            logger.warning(
                "Broker %r failed %d time(s) in a row (%s); %s, trying again every %gs",
                self.broker,
                self.failures,
                exc,
                self.meanwhile,
                self.cooldown,
            )

    def _observe(self, *, is_open: bool) -> None:
        """Report the state, if this breaker is the one the gauge shows."""
        if self.observed:
            observe_breaker(self.broker, is_open=is_open)


class Watched(Broker):
    """A broker whose answers feed a breaker; publishing goes through it.

    It keeps the broker only weakly: brokers belong to their event loop, and
    an idle publisher must not keep a closed loop's broker alive.
    """

    def __init__(self, broker: Broker, breaker: Breaker) -> None:
        """Watch ``broker`` for ``breaker``."""
        self.inner = ref(broker)
        self.breaker = breaker
        self.publish_timeout = broker.publish_timeout
        self.priorities = broker.priorities

    def _broker(self) -> Broker:
        """The broker watched; it lives as long as its loop."""
        broker = self.inner()
        if broker is None:  # Closed and dropped (shutdown) while publishing.
            raise BrokerUnavailable("The broker was closed.")
        return broker

    async def declare(self, queues: Iterable[str]) -> None:
        """Declare through the broker; only failures count."""
        await self._watch(self._broker().declare(queues), confirms=False)

    async def publish(
        self, queue: str, body: bytes, *, message_id: str, priority: int = 0
    ) -> None:
        """Publish through the broker; a confirmation closes the breaker.

        ``priority`` reaches only a broker that takes one (``priorities``).
        """
        call = publish_with_priority(
            self._broker(), queue, body, message_id=message_id, priority=priority
        )
        await self._watch(call, confirms=True)

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Consume through the broker (publishers never do)."""
        return await self._broker().consume(queues, prefetch, handler)

    async def check(self) -> None:
        """Check the broker; only failures count."""
        await self._watch(self._broker().check(), confirms=False)

    async def _watch(self, call: Awaitable[None], *, confirms: bool) -> None:
        """Await ``call`` and tell the breaker how it went."""
        try:
            await call
        except BrokerUnavailable as exc:
            self.breaker.failed(exc)
            raise
        except BrokerRefused:
            self.breaker.reached()
            raise
        if confirms:
            self.breaker.succeeded()
