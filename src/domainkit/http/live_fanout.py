"""Live events across processes, through PostgreSQL or Redis (ADR 0143).

``LIVE_FANOUT`` names a backend. Each process sends the shared events it
commits (``live_events.share``) from a background task of its event loop,
and each process with subscribers listens on the channel, then hands the
events the other processes sent to its subscribers. Delivery is best
effort, as in process: a listener that reconnects misses what was sent
meanwhile.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Final, Protocol

from sqlalchemy import text

from domainkit.conf import settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tasks import spawn

__all__ = ["Backend", "PostgresBackend", "RedisBackend", "Sender", "backend"]

logger = logging.getLogger(__name__)

NOTIFY_LIMIT: Final = 7999
"""The longest message PostgreSQL's ``NOTIFY`` carries, in bytes (under 8,000)."""
MAX_WAITING: Final = 10_000
"""Messages a loop holds for sending; past it, the newest are dropped, logged."""
BATCH: Final = 100
"""Messages sent in one statement (``pg_notify``) or one pipeline (``PUBLISH``)."""

type Heard = Callable[[str], None]


class Backend(Protocol):
    """Where shared events go between processes."""

    limit: int | None
    """The longest message it carries, in bytes; None for no limit worth one."""

    async def send(self, messages: Sequence[str]) -> None:
        """Send ``messages`` to every listening process, in order."""

    async def listen(self, heard: Heard) -> None:
        """Hand each message heard to ``heard``; raise once the connection is lost."""


class PostgresBackend:
    """``pg_notify`` on a database, and ``LISTEN`` on a connection of its own."""

    limit: int | None = NOTIFY_LIMIT

    def __init__(self, alias: str, channel: str) -> None:
        """Carry messages on ``channel`` of the database ``alias``."""
        self.alias = alias
        self.channel = channel

    async def send(self, messages: Sequence[str]) -> None:
        """Notify each message, in one statement and one transaction."""
        from domainkit.db import connections

        async with connections.engine(self.alias).begin() as connection:
            await connection.execute(
                text(
                    "SELECT pg_notify(:channel, message) "
                    "FROM unnest(CAST(:messages AS text[])) AS message"
                ),
                {"channel": self.channel, "messages": list(messages)},
            )

    async def listen(self, heard: Heard) -> None:
        """Listen until the connection is lost; never hand it back to the pool.

        Raises:
            ImproperlyConfigured: The database is not PostgreSQL through
                asyncpg, or goes through PgBouncer, whose transaction
                pooling keeps no session to listen on.
            ConnectionError: The connection was lost.
        """
        from domainkit.db import connections

        engine = connections.engine(self.alias)
        if (
            engine.dialect.name != "postgresql"
            or engine.dialect.driver != "asyncpg"
            or connections.config(self.alias).PGBOUNCER
        ):
            raise ImproperlyConfigured(
                f"LIVE_FANOUT listens on DATABASES[{self.alias!r}]: it must be "
                "PostgreSQL through asyncpg, and not through PgBouncer."
            )
        lost = asyncio.Event()
        async with engine.connect() as connection:
            try:
                raw: Any = (await connection.get_raw_connection()).driver_connection
                raw.add_termination_listener(lambda _: lost.set())
                await (
                    raw.add_listener(  # Called with connection, pid, channel, payload.
                        self.channel, lambda *args: heard(args[-1])
                    )
                )
                await lost.wait()
            finally:
                await connection.invalidate()
        raise ConnectionError("The connection listening for live events was lost.")


class RedisBackend:
    """``PUBLISH`` and a subscription on a cache's Redis (not a cluster)."""

    limit: int | None = None

    def __init__(self, alias: str, channel: str) -> None:
        """Carry messages on ``channel`` of the cache ``alias``."""
        self.alias = alias
        self.channel = channel

    def _client(self) -> Any:  # noqa: ANN401 - redis-py's client.
        """The cache's client.

        Raises:
            ImproperlyConfigured: The cache is not Redis, or is a cluster.
        """
        from domainkit.cache import caches
        from domainkit.cache.redis import ClusterStore, RedisStore

        store = caches[self.alias].store
        if not isinstance(store, RedisStore) or isinstance(store, ClusterStore):
            raise ImproperlyConfigured(
                f"LIVE_FANOUT uses CACHES[{self.alias!r}]: it must be Redis, "
                "and not a cluster."
            )
        return store.client

    async def send(self, messages: Sequence[str]) -> None:
        """Publish each message, in one pipeline."""
        pipeline = self._client().pipeline(transaction=False)
        for message in messages:
            pipeline.publish(self.channel, message)
        await pipeline.execute()

    async def listen(self, heard: Heard) -> None:
        """Hand each message of the channel to ``heard`` until the subscription ends.

        Raises:
            ConnectionError: The subscription ended.
        """
        subscription = self._client().pubsub()
        try:
            await subscription.subscribe(self.channel)
            async for message in subscription.listen():
                if message.get("type") == "message":
                    data = message["data"]
                    heard(data.decode() if isinstance(data, bytes) else str(data))
        finally:
            await subscription.aclose()
        raise ConnectionError("The subscription to live events ended.")


def backend() -> Backend | None:
    """The backend ``LIVE_FANOUT`` names, or None (in process)."""
    config = settings.LIVE_FANOUT
    if config.BACKEND == "postgres":
        return PostgresBackend(config.ALIAS, config.CHANNEL)
    if config.BACKEND == "redis":
        return RedisBackend(config.ALIAS, config.CHANNEL)
    return None


class Sender:
    """One event loop's messages for the other processes, sent in the background.

    ``put`` never waits: a task sends what waits, a batch at a time, and
    ends when nothing is left. A batch that fails is logged and dropped,
    as an event a reconnecting client missed.
    """

    def __init__(self, backend: Backend) -> None:
        """Send through ``backend``."""
        self.backend = backend
        self.waiting: deque[str] = deque()
        self._task: asyncio.Task[None] | None = None
        self._full = False

    def put(self, message: str, kind: str) -> None:
        """Send ``message`` (an event of ``kind``) soon; drop it if it cannot go."""
        limit = self.backend.limit
        if limit is not None and len(message.encode()) > limit:
            logger.warning(
                "A %s was not sent to the other processes: its message has more "
                "than %d bytes, the most a PostgreSQL notification holds; send "
                "it through Redis (LIVE_FANOUT), or share a smaller event",
                kind,
                limit,
            )
            return
        if len(self.waiting) >= MAX_WAITING:
            if not self._full:
                self._full = True
                logger.warning(
                    "Dropped live events for the other processes: %d already "
                    "wait to be sent (logged once until they are)",
                    len(self.waiting),
                )
            return
        self.waiting.append(message)
        if self._task is None or self._task.done():
            self._task = spawn(self._send_all(), name="domainkit.live.send")

    async def _send_all(self) -> None:
        """Send what waits, a batch at a time, until nothing does."""
        while self.waiting:
            batch = [
                self.waiting.popleft() for _ in range(min(BATCH, len(self.waiting)))
            ]
            try:
                await self.backend.send(batch)
            except Exception as exc:  # noqa: BLE001 - logged; best effort.
                logger.warning(
                    "Could not send %d live event(s) to the other processes: %s",
                    len(batch),
                    exc,
                )
        self._full = False
