"""NATS JetStream (``nats://``, ``nats+tls://``) as a work queue (the ``nats`` extra).

One stream, ``<PREFIX>``, holds the subjects ``<PREFIX>.<queue>`` with the
work-queue retention: a message stays until a consumer acknowledges it.
Each queue has a durable pull consumer, ``<queue>``, shared by every
worker: a message goes to one of them, and comes back to another when
it is not answered within ``REDELIVER_AFTER``. Publishing waits for
JetStream's acknowledgement, and sends the message id as
``Nats-Msg-Id``, so a message published twice is stored once. A
requeued message is negatively acknowledged, and handed out again.
"""

import asyncio
import ssl
from collections.abc import Iterable, Sequence
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from domainkit.broker.base import BrokerRefused, BrokerUnavailable
from domainkit.broker.pull import Fetched, PullBroker, Puller
from domainkit.conf import BrokerConfig

__all__ = ["NatsBroker"]

WAIT: Final = 1.0
"""Seconds a fetch waits for a message."""
MESSAGE_ID: Final = "Nats-Msg-Id"


class NatsBroker(PullBroker):
    """Work queues on NATS JetStream (see the module)."""

    def __init__(self, config: BrokerConfig, *, connect: Any = None) -> None:  # noqa: ANN401
        """Connect lazily to ``config.URL``; ``connect`` replaces ``nats.connect``."""
        super().__init__()
        self.config = config
        self.stream_name = str(config.PREFIX).replace(".", "_")
        self.publish_timeout = config.PUBLISH_TIMEOUT
        self._connect = connect
        self._connection: Any = None
        self._lock = asyncio.Lock()

    def subject(self, queue: str) -> str:
        """The subject of ``queue``."""
        return f"{self.config.PREFIX}.{queue}"

    async def jetstream(self) -> Any:  # noqa: ANN401 - nats-py's JetStreamContext.
        """JetStream, over a connection made on first use."""
        async with self._lock:
            if self._connection is None:
                connect = self._connect
                if connect is None:
                    import nats

                    connect = nats.connect
                config = self.config
                url = config.URL.get_secret_value()
                parts = urlsplit(url)
                options: dict[str, Any] = {
                    "servers": [urlunsplit(parts._replace(scheme="tls"))]
                    if config.scheme == "nats+tls"
                    else [url],
                    "connect_timeout": config.CONNECT_TIMEOUT,
                    "allow_reconnect": True,
                    "max_reconnect_attempts": -1,
                }
                if config.scheme == "nats+tls":
                    options["tls"] = _tls(config)
                try:
                    # nats-py retries a first connection too: bound it.
                    async with asyncio.timeout(config.CONNECT_TIMEOUT):
                        self._connection = await connect(**options)
                except (OSError, TimeoutError) as exc:
                    raise BrokerUnavailable("NATS could not be reached.") from exc
                except Exception as exc:
                    raise BrokerUnavailable(
                        f"NATS could not be reached ({type(exc).__name__})."
                    ) from exc
            return self._connection.jetstream()

    async def declare(self, queues: Iterable[str]) -> None:
        """The stream (work-queue retention), and a durable consumer per queue."""
        from nats.js.api import (
            AckPolicy,
            ConsumerConfig,
            RetentionPolicy,
            StreamConfig,
        )
        from nats.js.errors import BadRequestError

        js = await self.jetstream()
        names = list(queues)
        try:
            async with asyncio.timeout(self.config.CONNECT_TIMEOUT):
                try:
                    await js.add_stream(
                        StreamConfig(
                            name=self.stream_name,
                            subjects=[f"{self.config.PREFIX}.>"],
                            retention=RetentionPolicy.WORK_QUEUE,
                        )
                    )
                except BadRequestError as exc:
                    if "already in use" not in str(exc):
                        raise BrokerRefused("NATS refused the stream.") from exc
                for queue in names:
                    await js.add_consumer(
                        self.stream_name,
                        ConsumerConfig(
                            durable_name=_durable(queue),
                            filter_subject=self.subject(queue),
                            ack_policy=AckPolicy.EXPLICIT,
                            ack_wait=self.config.REDELIVER_AFTER,
                        ),
                    )
        except TimeoutError as exc:
            raise BrokerUnavailable("NATS did not answer in time (declare).") from exc

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Publish, and wait for JetStream to have stored it."""
        from nats.errors import NoRespondersError
        from nats.errors import TimeoutError as NatsTimeout
        from nats.js.errors import NoStreamResponseError

        js = await self.jetstream()
        try:
            await js.publish(
                self.subject(queue),
                body,
                timeout=self.publish_timeout,
                headers={MESSAGE_ID: message_id},
            )
        except (NoStreamResponseError, NoRespondersError) as exc:
            raise BrokerRefused(f"No stream takes the queue {queue!r}.") from exc
        except (NatsTimeout, TimeoutError) as exc:
            raise BrokerUnavailable("NATS did not confirm in time (publish).") from exc

    async def puller(self, queues: Sequence[str]) -> Puller:
        """A puller bound to each queue's durable consumer."""
        js = await self.jetstream()
        subscriptions = [
            (
                queue,
                await js.pull_subscribe_bind(_durable(queue), stream=self.stream_name),
            )
            for queue in queues
        ]
        return _NatsPuller(subscriptions)

    async def check(self) -> None:
        """Account information: JetStream answers."""
        js = await self.jetstream()
        try:
            async with asyncio.timeout(self.config.CONNECT_TIMEOUT):
                await js.account_info()
        except Exception as exc:
            raise BrokerUnavailable(
                f"NATS JetStream did not answer ({type(exc).__name__})."
            ) from exc

    async def close(self) -> None:
        """Wait for answers, then drain the connection."""
        await super().close()
        if self._connection is not None:
            connection, self._connection = self._connection, None
            await connection.close()


class _NatsPuller(Puller):
    """Fetches from each queue's consumer, in turn."""

    def __init__(self, subscriptions: list[tuple[str, Any]]) -> None:
        """Ask ``subscriptions``, (queue, pull subscription) pairs."""
        self.subscriptions = subscriptions
        self.turn = 0

    async def fetch(self, limit: int) -> Sequence[Fetched]:
        """The next queue's messages, waiting a second over all of them."""
        from nats.errors import TimeoutError as NatsTimeout

        wait = WAIT / len(self.subscriptions)
        for _ in range(len(self.subscriptions)):
            queue, subscription = self.subscriptions[self.turn]
            self.turn = (self.turn + 1) % len(self.subscriptions)
            try:
                messages = await subscription.fetch(batch=limit, timeout=wait)
            except (NatsTimeout, TimeoutError):
                continue
            if messages:
                return [
                    Fetched(
                        queue,
                        bytes(message.data),
                        (message.headers or {}).get(MESSAGE_ID, ""),
                        message,
                    )
                    for message in messages
                ]
        return []

    async def ack(self, message: Fetched) -> None:
        """Acknowledge: the stream forgets it."""
        await message.handle.ack()

    async def requeue(self, message: Fetched) -> None:
        """Negatively acknowledge: handed out again."""
        await message.handle.nak()

    async def close(self) -> None:
        """Unsubscribe the pull subscriptions."""
        for _, subscription in self.subscriptions:
            await subscription.unsubscribe()


def _durable(queue: str) -> str:
    """A consumer name for ``queue``: NATS names take no dots."""
    return queue.replace(".", "_")


def _tls(config: BrokerConfig) -> ssl.SSLContext:
    """A verifying context with the configured CA and client certificate."""
    context = ssl.create_default_context(
        cafile=None if config.TLS_CA is None else str(config.TLS_CA)
    )
    if config.TLS_CERT is not None and config.TLS_KEY is not None:
        context.load_cert_chain(str(config.TLS_CERT), str(config.TLS_KEY))
    return context
