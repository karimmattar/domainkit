"""Which read replicas read-only work may read now, from lag read in the background.

A replica with ``MAX_LAG`` (seconds) is read only while it is known to be
reachable and within that many seconds of its primary; otherwise
read-only work on the primary reads the primary (ADR 0159). A replica
without ``MAX_LAG`` is read as before, however far behind it is.

Each event loop keeps a reading per replica: how far behind it was, and
as of when. A replica is read while the reading's lag plus its age is
within ``MAX_LAG``: a replica that stopped replaying since cannot be
further behind than that. Read-only work that finds a reading older than
``check_interval`` (a second, or half of ``MAX_LAG`` if that is less)
starts a probe in the background (``domainkit.tasks.spawn``), one at a
time per replica, and never waits for it. So a process that reads probes
each replica about once a second, and an idle one never does; the first
read after a quiet spell longer than ``MAX_LAG`` reads the primary while
its probe runs.

On PostgreSQL a probe asks the replica, in one round trip, whether it is
in recovery and how old the last transaction it replayed is
(``pg_last_xact_replay_timestamp()``). A server not in recovery is its
own primary: it is never behind (tests point a replica at the primary).
That age also grows while the primary commits nothing, so when it passes
``MAX_LAG`` the probe asks the primary where its WAL ends
(``pg_current_wal_lsn()``), then the replica whether it replayed that far
(``pg_last_wal_replay_lsn()``): if it did, it was up to date when the
primary answered. On SQLite a probe only checks that the replica answers.

A probe that fails, or takes over ``CHECK_TIMEOUT``, leaves the replica
unread until one succeeds. Each change, reads leaving a replica or coming
back to it, is logged and counted (``db_replica_switches_total``); each
lag read is ``db_replica_lag_seconds``.

After a unit of work commits on a primary (``note_commit``), read-only
work later in the same task reads a replica of it that has ``MAX_LAG``
only once a reading shows that the replica had replayed that commit: a
request reads what it wrote, as Laravel's ``sticky`` and Rails' ``delay``
let it.
"""

import asyncio
import logging
import math
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit import clock
from domainkit.conf import DatabaseConfig, settings
from domainkit.db.connections import INTERNAL, connections
from domainkit.tasks import spawn
from domainkit.utils.loops import PerLoop

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_switch(replica: str, to: str) -> None:
        """Count nothing: ``prometheus_client`` is not installed."""

    def observe_lag(replica: str, lag: float) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

else:
    SWITCHES: Final = Counter(
        "db_replica_switches_total",
        "Read-only work that left a read replica for its primary (to=primary), "
        "or came back to it (to=replica).",
        ["database", "to"],
    )
    LAG: Final = Gauge(
        "db_replica_lag_seconds",
        "How far behind its primary a read replica with MAX_LAG was when last read.",
        ["database"],
    )

    def observe_switch(replica: str, to: str) -> None:
        """Count reads leaving ``replica`` (``to="primary"``) or coming back."""
        SWITCHES.labels(replica, to).inc()

    def observe_lag(replica: str, lag: float) -> None:
        """Record the lag just read of ``replica``, if it is known."""
        if math.isfinite(lag):
            LAG.labels(replica).set(lag)


CHECK_EVERY: Final = 1.0
"""Seconds after which reading a replica starts a new probe of its lag (at
most half its ``MAX_LAG``)."""
CHECK_TIMEOUT: Final = 5.0
"""Seconds a probe may take; past them the replica counts as out of reach."""
PROBE_TASK: Final = "domainkit.db.replica_lag"
"""The name of a probe's task (``domainkit.tasks.spawn``)."""
REPLAY_AGE: Final = text(
    "SELECT pg_is_in_recovery(), EXTRACT(EPOCH FROM clock_timestamp() "
    "- pg_last_xact_replay_timestamp())::float8"
).execution_options(**{INTERNAL: True})
"""Whether the server replays a primary's WAL, and how many seconds ago the
last transaction it replayed committed there (NULL before the first)."""
WAL_END: Final = text("SELECT pg_current_wal_lsn()::text").execution_options(
    **{INTERNAL: True}
)
"""Where the primary's WAL ends: what a replica that replayed it all has."""
REPLAYED: Final = text(
    "SELECT CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() "
    "ELSE pg_current_wal_lsn() END >= CAST(CAST(:position AS text) AS pg_lsn)"
).execution_options(**{INTERNAL: True})
"""Whether the replica replayed the WAL up to ``position``; a server not in
recovery compares where its own WAL ends."""
ANSWERS: Final = text("SELECT 1").execution_options(**{INTERNAL: True})


@dataclass(frozen=True, slots=True)
class Reading:
    """What a probe found: how far behind a replica was, and as of when."""

    config: DatabaseConfig
    """The replica's settings when it was read; a reading under others is void."""
    at: float
    """The ``clock.monotonic()`` as of which the replica was ``lag`` behind."""
    lag: float
    """Seconds behind its primary; ``math.inf`` when unknown or out of reach."""
    error: BaseException | None = None
    """Why the lag could not be read, if it could not."""


@dataclass(slots=True)
class _LoopReplicas:
    """One event loop's readings, its probes, and whether each replica is read."""

    readings: dict[str, Reading] = field(default_factory=dict)
    probes: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    read: dict[str, bool] = field(default_factory=dict)
    """Whether the last reading let reads go to the replica, for the log."""


_loops: Final[PerLoop[_LoopReplicas]] = PerLoop()
_written: ContextVar[dict[str, float] | None] = ContextVar(
    "domainkit_replica_written", default=None
)
"""When work in this context last committed on each primary (``note_commit``)."""


def check_interval(max_lag: float) -> float:
    """Seconds after which a replica's reading is read again."""
    return min(CHECK_EVERY, max_lag / 2)


def readable(replica: str, primary: str) -> bool:
    """Whether read-only work on ``primary`` may read ``replica`` now.

    One without ``MAX_LAG`` always may. One with it may while its last
    reading's lag plus age is within ``MAX_LAG``, and, once this task
    committed on ``primary``, the reading shows that commit replayed. A
    reading older than ``check_interval`` starts a probe in the
    background. Without a running event loop nothing is known: False.
    """
    config = settings.DATABASES[replica]
    max_lag = config.MAX_LAG
    if max_lag is None:
        return True
    try:
        state = _loops.get(_LoopReplicas)
    except RuntimeError:  # No loop runs here: no reading, and no probe.
        return False
    now = clock.monotonic()
    reading = state.readings.get(replica)
    if reading is not None and reading.config is not config:
        reading = None  # Read under other settings.
    if reading is None or now - reading.at >= check_interval(max_lag):
        probe = state.probes.get(replica)
        if probe is None or probe.done():
            state.probes[replica] = spawn(
                _probe(state, replica, primary, config, max_lag), name=PROBE_TASK
            )
    if reading is None or reading.lag + (now - reading.at) > max_lag:
        return False
    written = _written.get()
    since = None if written is None else written.get(primary)
    return since is None or reading.at - reading.lag >= since


def note_commit(primary: str) -> None:
    """Note that work in this context committed on ``primary`` just now.

    Read-only work later in the same task reads a replica of ``primary``
    with ``MAX_LAG`` only once it is known to have replayed this commit.
    """
    written = _written.get()
    _written.set({**(written or {}), primary: clock.monotonic()})


async def measure_lag(
    replica: str, primary: str, max_lag: float
) -> tuple[float, float]:
    """How many seconds ``replica`` is behind ``primary``, and as of when.

    Returns the lag (``math.inf`` when it cannot be told) and the
    ``clock.monotonic()`` as of which it holds. Past ``max_lag``, the age
    of the last transaction replayed is checked against the primary's WAL,
    which tells a primary that commits nothing from a replica behind it.

    Raises:
        Exception: Either database could not be reached, or refused a
            statement.
    """
    started = clock.monotonic()
    engine = connections.engine(replica)
    if engine.dialect.name != "postgresql":
        async with engine.connect() as connection:
            await connection.execute(ANSWERS)
        return 0.0, started
    async with connections.autocommit(replica) as connection:
        recovering, age = (await connection.execute(REPLAY_AGE)).one()
    if not recovering:
        return 0.0, started
    if age is not None and age <= max_lag:
        return age, started
    asked = clock.monotonic()
    async with connections.autocommit(primary) as connection:
        position = await connection.scalar(WAL_END)
    async with connections.autocommit(replica) as connection:
        if await _replayed(connection, position):
            return 0.0, asked
    return (math.inf if age is None else age), started


async def _replayed(connection: AsyncConnection, position: str | None) -> bool:
    """Whether the server of ``connection`` replayed the WAL up to ``position``."""
    return bool(await connection.scalar(REPLAYED, {"position": position}))


async def _probe(
    state: _LoopReplicas,
    replica: str,
    primary: str,
    config: DatabaseConfig,
    max_lag: float,
) -> None:
    """Read how far behind ``replica`` is, keep the reading, and log a change."""
    started = clock.monotonic()
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            lag, at = await measure_lag(replica, primary, max_lag)
    except Exception as error:  # noqa: BLE001 - out of reach, refused, or too slow.
        reading = Reading(config, started, math.inf, error)
    else:
        reading = Reading(config, at, lag)
        observe_lag(replica, lag)
    state.readings[replica] = reading
    read = reading.lag <= max_lag
    was_read = state.read.get(replica, True)
    state.read[replica] = read
    if read is was_read:
        return
    observe_switch(replica, "replica" if read else "primary")
    if read:
        logger.info(
            "Read-only work on %r reads replica %r again: it is %.3g seconds behind",
            primary,
            replica,
            reading.lag,
        )
    elif reading.error is not None:
        logger.warning(
            "Read-only work on %r reads the primary, not replica %r: its lag "
            "could not be read",
            primary,
            replica,
            exc_info=reading.error,
        )
    elif reading.lag == math.inf:
        logger.warning(
            "Read-only work on %r reads the primary, not replica %r: its lag is "
            "unknown (no transaction replayed since it started, and not all of "
            "the primary's WAL)",
            primary,
            replica,
        )
    else:
        logger.warning(
            "Read-only work on %r reads the primary, not replica %r: it is %.3g "
            "seconds behind, over its MAX_LAG of %g",
            primary,
            replica,
            reading.lag,
            max_lag,
        )
