"""Redis Streams (``redis://``, ``rediss://``) as a work queue (the ``redis`` extra).

Each queue is a stream, ``<PREFIX>:<queue>``, read by one consumer
group, ``workers``: a message goes to one consumer of the group, which
acknowledges it (``XACK``, then ``XDEL``) once its work is recorded. A
message taken and not answered within ``REDELIVER_AFTER`` (its consumer
went away) is claimed by another (``XAUTOCLAIM``). A requeued message is
added again and the taken one acknowledged, in one transaction. Streams
are not trimmed: an answered message is deleted, so a stream holds the
messages still to do.
"""

import asyncio
import os
import socket
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Final

from domainkit.broker.base import BrokerRefused, BrokerUnavailable
from domainkit.broker.pull import Fetched, PullBroker, Puller
from domainkit.conf import BrokerConfig

__all__ = ["RedisStreamsBroker"]

GROUP: Final = "workers"
WAIT_MS: Final = 1000
"""How long a fetch waits for a message, in milliseconds."""


class RedisStreamsBroker(PullBroker):
    """Work queues on Redis Streams (see the module)."""

    def __init__(self, config: BrokerConfig, *, client: Any = None) -> None:  # noqa: ANN401 - redis's.
        """Connect lazily to ``config.URL``; ``client`` replaces it (tests)."""
        super().__init__()
        self.config = config
        self.prefix = config.PREFIX
        self.publish_timeout = config.PUBLISH_TIMEOUT
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._client = client

    @property
    def client(self) -> Any:  # noqa: ANN401 - redis.asyncio.Redis.
        """The connection pool, made on first use."""
        if self._client is None:
            from redis.asyncio import Redis

            config = self.config
            options: dict[str, Any] = {
                "socket_connect_timeout": config.CONNECT_TIMEOUT,
                "socket_timeout": config.PUBLISH_TIMEOUT + WAIT_MS / 1000,
            }
            if config.scheme == "rediss":
                options.update(
                    ssl_ca_certs=None if config.TLS_CA is None else str(config.TLS_CA),
                    ssl_certfile=(
                        None if config.TLS_CERT is None else str(config.TLS_CERT)
                    ),
                    ssl_keyfile=None if config.TLS_KEY is None else str(config.TLS_KEY),
                )
            self._client = Redis.from_url(config.URL.get_secret_value(), **options)
        return self._client

    def stream(self, queue: str) -> str:
        """The stream of ``queue``."""
        return f"{self.prefix}:{queue}"

    async def declare(self, queues: Iterable[str]) -> None:
        """Make each stream and its group, reading from the first message."""
        from redis.exceptions import ResponseError

        for queue in queues:
            async with _reached("declare"):
                try:
                    await self.client.xgroup_create(
                        self.stream(queue), GROUP, id="0", mkstream=True
                    )
                except ResponseError as exc:
                    if "BUSYGROUP" not in str(exc):
                        raise BrokerRefused(
                            f"Redis refused the stream {queue!r}."
                        ) from exc

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """``XADD`` the message; Redis has it once the call returns."""
        async with _reached("publish"), asyncio.timeout(self.publish_timeout):
            await self.client.xadd(self.stream(queue), {"body": body, "id": message_id})

    async def puller(self, queues: Sequence[str]) -> Puller:
        """A puller reading ``queues``' streams."""
        return _RedisPuller(self, queues)

    async def check(self) -> None:
        """``PING``."""
        async with _reached("check"), asyncio.timeout(self.config.CONNECT_TIMEOUT):
            await self.client.ping()

    async def close(self) -> None:
        """Wait for answers, then close the pool."""
        await super().close()
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class _RedisPuller(Puller):
    """Claims stale messages, then reads new ones, of a subscription's streams."""

    def __init__(self, broker: RedisStreamsBroker, queues: Sequence[str]) -> None:
        """Read ``queues`` of ``broker``."""
        self.broker = broker
        self.queues = list(queues)
        self.idle_ms = int(broker.config.REDELIVER_AFTER * 1000)
        self.buffer: list[Fetched] = []
        """Messages taken beyond a fetch's limit (``COUNT`` is per stream):
        this consumer's already, handed out by the next fetches."""

    async def fetch(self, limit: int) -> Sequence[Fetched]:
        """Messages taken before, others left, else new ones (waiting a second)."""
        if not self.buffer:
            self.buffer = list(await self._read(limit))
        found, self.buffer = self.buffer[:limit], self.buffer[limit:]
        return found

    async def _read(self, limit: int) -> Sequence[Fetched]:
        """Claimed messages of one stream, else new ones of every stream."""
        client = self.broker.client
        async with _reached("fetch"):
            for queue in self.queues:
                claimed = await client.xautoclaim(
                    self.broker.stream(queue),
                    GROUP,
                    self.broker.consumer,
                    min_idle_time=self.idle_ms,
                    start_id="0-0",
                    count=limit,
                )
                if found := [self._fetched(queue, e) for e in claimed[1] if e[1]]:
                    return found
            answer = await client.xreadgroup(
                GROUP,
                self.broker.consumer,
                {self.broker.stream(q): ">" for q in self.queues},
                count=limit,
                block=WAIT_MS,
            )
        streams = {self.broker.stream(q): q for q in self.queues}
        return [
            self._fetched(streams[_text(stream)], entry)
            for stream, entries in answer or []
            for entry in entries
        ]

    def _fetched(self, queue: str, entry: Any) -> Fetched:  # noqa: ANN401 - redis's.
        """A stream entry as a message."""
        entry_id, fields = entry
        fields = {_text(key): value for key, value in fields.items()}
        return Fetched(
            queue,
            bytes(fields.get("body", b"")),
            _text(fields.get("id", b"")),
            entry_id,
        )

    async def ack(self, message: Fetched) -> None:
        """``XACK`` and ``XDEL``, together."""
        stream = self.broker.stream(message.queue)
        async with (
            _reached("ack"),
            self.broker.client.pipeline(transaction=True) as pipe,
        ):
            pipe.xack(stream, GROUP, message.handle)
            pipe.xdel(stream, message.handle)
            await pipe.execute()

    async def requeue(self, message: Fetched) -> None:
        """Added again, and the taken one answered, together."""
        stream = self.broker.stream(message.queue)
        async with (
            _reached("requeue"),
            self.broker.client.pipeline(transaction=True) as pipe,
        ):
            pipe.xadd(stream, {"body": message.body, "id": message.message_id})
            pipe.xack(stream, GROUP, message.handle)
            pipe.xdel(stream, message.handle)
            await pipe.execute()


def _text(value: Any) -> str:  # noqa: ANN401 - bytes or str.
    """Redis's bytes as text."""
    return value.decode() if isinstance(value, bytes) else str(value)


class _reached:
    """Turn Redis's connection failures into ``BrokerUnavailable``."""

    def __init__(self, action: str) -> None:
        """Name ``action`` in the error."""
        self.action = action

    async def __aenter__(self) -> None:
        """Nothing to do before."""

    async def __aexit__(
        self, kind: type | None, exc: BaseException | None, tb: object
    ) -> bool:
        """Raise ``BrokerUnavailable`` for a connection failure or a timeout."""
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeout

        if isinstance(exc, (RedisConnectionError, RedisTimeout, TimeoutError, OSError)):
            raise BrokerUnavailable(
                f"Redis could not be reached ({self.action})."
            ) from exc
        return False
