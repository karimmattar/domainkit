"""Brokers per ``BROKERS`` alias (the broker counterpart of ``caches``).

AMQP connections, and the in-memory broker's queues, belong to the event
loop that opened them, so each running loop gets its own brokers.
"""

import asyncio
import threading
from collections.abc import Callable
from typing import Final
from weakref import WeakKeyDictionary

from domainkit.broker.base import Broker
from domainkit.broker.memory import MemoryBroker
from domainkit.conf import BrokerConfig, settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.closing import close_each

type BrokerFactory = Callable[[BrokerConfig], Broker]


def rabbitmq_broker(config: BrokerConfig) -> Broker:
    """Connect to RabbitMQ; needs ``domainkit[rabbitmq]``."""
    try:
        from domainkit.broker.rabbitmq import RabbitMQBroker
    except ImportError as exc:  # pragma: no cover - only without the [rabbitmq] extra.
        raise ImproperlyConfigured(
            "RabbitMQ brokers need the rabbitmq extra: "
            "pip install 'domainkit[rabbitmq]'."
        ) from exc
    return RabbitMQBroker(config)


class BrokerHandler:
    """Create each configured broker on first use, per event loop."""

    def __init__(self, connect: BrokerFactory = rabbitmq_broker) -> None:
        """Use ``connect`` to build RabbitMQ brokers (tests may replace it)."""
        self._connect = connect
        self._brokers: WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, tuple[BrokerConfig, Broker]]
        ] = WeakKeyDictionary()
        self._replaced: WeakKeyDictionary[asyncio.AbstractEventLoop, list[Broker]] = (
            WeakKeyDictionary()
        )
        self._lock = threading.Lock()
        """Held to change or walk the registries: loops on other threads do too."""

    def config(self, alias: str) -> BrokerConfig:
        """Return the ``BROKERS`` entry of ``alias``."""
        try:
            return settings.BROKERS[alias]
        except KeyError:
            raise ImproperlyConfigured(f"BROKERS has no {alias!r} alias.") from None

    def __getitem__(self, alias: str) -> Broker:
        """The running loop's broker for ``alias``; new if its settings changed."""
        config = self.config(alias)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                f"brokers[{alias!r}] needs a running event loop: connections "
                "belong to one, so look brokers up inside async code."
            ) from None
        with self._lock:
            if loop not in self._brokers:
                self._forget_closed_loops()
            brokers = self._brokers.setdefault(loop, {})
        current = brokers.get(alias)
        if current is not None and current[0] == config:
            return current[1]
        if current is not None:  # override_settings changed it; close it later.
            with self._lock:
                replaced = self._replaced.setdefault(loop, [])
            replaced.append(current[1])
        broker = MemoryBroker() if config.scheme == "memory" else self._connect(config)
        brokers[alias] = (config, broker)
        return broker

    def _forget_closed_loops(self) -> None:
        """Drop the brokers of loops closed without ``close_all``.

        Their connections died with the loop, and a broker that references
        its loop would keep it alive for good. Called with ``_lock`` held.
        """
        for loop in [loop for loop in self._brokers if loop.is_closed()]:
            self._brokers.pop(loop, None)
            self._replaced.pop(loop, None)

    async def close_all(self) -> None:
        """Close the running loop's brokers; the next use opens new ones.

        Each broker's own ``close``: ``Broker.close`` is the base class's,
        which closes nothing.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            current = [broker for _, broker in self._brokers.pop(loop, {}).values()]
            replaced = self._replaced.pop(loop, [])
        await close_each([*current, *replaced], lambda broker: broker.close())


brokers: Final = BrokerHandler()
"""The process-wide brokers: ``brokers["default"]``."""
