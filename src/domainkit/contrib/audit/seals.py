"""Seal the audit trail into a hash chain, anchor its head on a ledger, verify.

``seal`` gives every row not sealed yet the next number of a chain. Its
digest is the SHA-256 of the row's canonical JSON; its link, the SHA-256 of
the previous link and the digest. Changing, removing, or reordering a sealed
row breaks the chain, but whoever can rewrite the tables can rewrite the
chain as well. So ``anchor`` also writes the chain's head (its number and
link) to a ledger (``AUDIT.LEDGER``, immudb), which keeps every revision and
verifies what it returns: a chain rewritten after a head was anchored no
longer matches it.

``verify`` reports changed and missing rows, links that do not follow,
anchors the chain no longer matches, seals left unanchored, and rows left
unsealed.

Rows are sealed after they commit, by the worker every ``AUDIT.SEAL_EVERY``
or by ``auditseal``, so a row can be rewritten unnoticed until it is sealed
and its head anchored. The newest sealed row is the high-water mark: a run
looks for unsealed rows from ``AUDIT.SEAL_LATE_WINDOW`` before it, for rows
that committed late, then on from the last row it sealed, so a run costs
what is new plus that window, however long the trail. A row that committed
later still stays unsealed, and ``verify`` says so once it is ``LOOKBACK``
old. A run seals ``AUDIT.SEAL_BATCHES`` batches at most; what is left waits
for the next run, and is counted (``audit_unsealed_rows``) and logged when
it passes ``AUDIT.SEAL_BACKLOG_WARNING``.
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, func, insert, literal, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit import clock
from domainkit.apps import registry
from domainkit.db import connections
from domainkit.ledger import Ledger, LedgerKeyNotFound, ledgers

from .personal import audited_databases, prepare_anonymous_keys
from .settings import AuditSettings
from .tables import audit_log, audit_seals

try:
    from prometheus_client import Gauge
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def _observe_backlog(database: str, rows: int) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

else:
    UNSEALED: Final = Gauge(
        "audit_unsealed_rows",
        "Audit rows a sealing run left for the next one (0: it caught up).",
        ["database"],
    )

    def _observe_backlog(database: str, rows: int) -> None:
        """Set the backlog gauge of ``database``."""
        UNSEALED.labels(database).set(rows)


logger = logging.getLogger(__name__)

GENESIS: Final = "0" * 64
"""The link before the first seal."""
LOOKBACK: Final = timedelta(days=1)
"""Rows this much older than now without a seal count as unsealed (``verify``)."""
BATCH: Final = 1000
"""Rows sealed per transaction."""
BACKLOG_COUNT_LIMIT: Final = 1_000_000
"""Counting a backlog stops here: past it, the gauge reads this much."""
COLUMNS: Final = (
    "id",
    "occurred_at",
    "actor",
    "action",
    "outcome",
    "target",
    "request_id",
    "client_ip",
    "event_id",
    "details",
)
"""The columns a row's digest covers."""
_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtext('domainkit.audit.seal'))")


@dataclass(frozen=True, slots=True)
class Head:
    """The newest seal of a chain: its number and link."""

    seq: int
    link: str


@dataclass(frozen=True, slots=True)
class Anchor:
    """A head written to the ledger, and when."""

    seq: int
    link: str
    anchored_at: datetime

    def encode(self) -> bytes:
        """The ledger value: canonical JSON."""
        return _canonical(
            {
                "seq": self.seq,
                "link": self.link,
                "anchored_at": self.anchored_at.isoformat(),
            }
        )

    @classmethod
    def decode(cls, value: bytes) -> "Anchor":
        """Read a ledger value back."""
        document = json.loads(value)
        return cls(
            int(document["seq"]),
            str(document["link"]),
            datetime.fromisoformat(document["anchored_at"]),
        )


@dataclass(slots=True)
class Report:
    """What ``verify`` found in one database's trail."""

    database: str
    sealed: int = 0
    """Seals checked."""
    anchors: int = 0
    """Anchors checked."""
    changed: list[UUID] = field(default_factory=list)
    """Sealed rows whose content no longer matches their digest."""
    missing: list[UUID] = field(default_factory=list)
    """Sealed rows no longer in the trail."""
    broken: list[int] = field(default_factory=list)
    """Seals whose number or link does not follow the one before."""
    rewritten: list[int] = field(default_factory=list)
    """Anchored heads the chain no longer has."""
    unanchored: int = 0
    """Seals after the newest anchor, sealed more than ``ANCHOR_EVERY`` and
    ``LOOKBACK`` ago: anchoring stopped (checked with a ledger only)."""
    unsealed: int = 0
    """Rows older than ``LOOKBACK`` without a seal."""

    @property
    def intact(self) -> bool:
        """Whether nothing was found wrong."""
        problems = (self.changed, self.missing, self.broken, self.rewritten)
        return not (any(problems) or self.unanchored or self.unsealed)


def digest(row: Mapping[Any, Any]) -> str:
    """The SHA-256 of the row's canonical JSON."""
    return hashlib.sha256(
        _canonical({name: _plain(row[name]) for name in COLUMNS})
    ).hexdigest()


def link(previous: str, row_digest: str) -> str:
    """The link after ``previous`` for a row with ``row_digest``."""
    return hashlib.sha256(f"{previous}{row_digest}".encode()).hexdigest()


def anchor_key(database: str) -> bytes:
    """The ledger key holding ``database``'s anchors, one revision each."""
    return f"domainkit/audit/{database}".encode()


async def seal(
    database: str,
    *,
    batch: int = BATCH,
    batches: int | None = None,
    late_window: timedelta | None = None,
) -> int:
    """Seal the rows of ``database``'s trail not sealed yet; count them.

    At most ``batches`` transactions of ``batch`` rows (default:
    ``AUDIT.SEAL_BATCHES``); late rows are looked for ``late_window``
    before the newest sealed row (default: ``AUDIT.SEAL_LATE_WINDOW``).
    """
    config = registry.settings(AuditSettings)
    batches = config.SEAL_BATCHES if batches is None else batches
    late_window = config.SEAL_LATE_WINDOW if late_window is None else late_window
    sealed = 0
    after: _Mark | None = None
    for _ in range(batches):
        async with connections.engine(database).begin() as connection:
            if connection.dialect.name == "postgresql":
                await connection.execute(_LOCK)  # One sealer per database.
            rows, after = await _seal_batch(connection, batch, late_window, after)
        sealed += rows
        if rows < batch:
            _observe_backlog(database, 0)
            return sealed
    await _report_backlog(database, late_window, config.SEAL_BACKLOG_WARNING)
    return sealed


async def unsealed_backlog(database: str, late_window: timedelta) -> int:
    """Rows sealing would still seal now: unsealed, within reach of the window.

    Counted up to ``BACKLOG_COUNT_LIMIT``.
    """
    async with connections.engine(database).connect() as connection:
        head = await _head(connection)
        waiting = _unsealed_rows().with_only_columns(audit_log.c.id)
        horizon = await _horizon(connection, head, clock.now(), late_window)
        if horizon is not None:
            waiting = waiting.where(audit_log.c.occurred_at >= horizon)
        count = await connection.scalar(
            select(func.count()).select_from(
                waiting.limit(BACKLOG_COUNT_LIMIT).subquery()
            )
        )
    return int(count or 0)


async def anchor(
    database: str,
    ledger: Ledger,
    *,
    every: timedelta,
    now: datetime | None = None,
) -> Anchor | None:
    """Write ``database``'s head to ``ledger``; None when there is nothing new.

    A head is anchored at most once per ``every``, and never twice. An anchor
    dated after ``now`` (written by a clock running ahead) holds none back.
    """
    now = now or clock.now()
    async with connections.engine(database).connect() as connection:
        head = await _head(connection)
    if head is None:
        return None
    try:
        latest = Anchor.decode((await ledger.read(anchor_key(database))).value)
    except LedgerKeyNotFound:
        latest = None
    if latest is not None and (
        latest.seq == head.seq or timedelta(0) <= now - latest.anchored_at < every
    ):
        return None
    written = Anchor(head.seq, head.link, now)
    await ledger.append(anchor_key(database), written.encode())
    return written


async def seal_all() -> dict[str, tuple[int, Anchor | None]]:
    """Seal, then anchor, every audited database; what each one got.

    First it makes the keys of today's and tomorrow's anonymous callers, so
    at midnight no audited request waits for another's insert of the day's
    key (see ``prepare_anonymous_keys``).
    """
    config = registry.settings(AuditSettings)
    done: dict[str, tuple[int, Anchor | None]] = {}
    for database in audited_databases():
        await prepare_anonymous_keys(database)
        sealed = await seal(database)
        anchored = None
        if config.LEDGER is not None:
            anchored = await anchor(
                database, ledgers[config.LEDGER], every=config.ANCHOR_EVERY
            )
        done[database] = (sealed, anchored)
    return done


async def verify(
    database: str,
    ledger: Ledger | None = None,
    *,
    now: datetime | None = None,
    every: timedelta | None = None,
) -> Report:
    """Check ``database``'s chain against its rows, and against ``ledger``.

    With a ledger, seals sealed more than ``every`` (default:
    ``AUDIT.ANCHOR_EVERY``) and ``LOOKBACK`` ago must be anchored.
    """
    now = now or clock.now()
    report = Report(database)
    anchored = await _anchors(database, ledger) if ledger is not None else {}
    report.anchors = len(anchored)
    newest_anchored = max(anchored, default=0)
    overdue = None  # Sealed before this, a seal must be anchored.
    if ledger is not None:
        if every is None:
            every = registry.settings(AuditSettings).ANCHOR_EVERY
        overdue = now - every - LOOKBACK
    previous = Head(0, GENESIS)
    query = (
        select(audit_seals, *(audit_log.c[name] for name in COLUMNS))
        .outerjoin(audit_log, audit_log.c.id == audit_seals.c.entry_id)
        .order_by(audit_seals.c.seq)
    )
    async with connections.engine(database).connect() as connection:
        async for row in await connection.stream(query):
            seal_row = row._mapping
            report.sealed += 1
            if seal_row["seq"] != previous.seq + 1 or seal_row["link"] != link(
                previous.link, seal_row["digest"]
            ):
                report.broken.append(seal_row["seq"])
            if seal_row["id"] is None:
                report.missing.append(seal_row["entry_id"])
            elif digest(seal_row) != seal_row["digest"]:
                report.changed.append(seal_row["entry_id"])
            if anchored.pop(seal_row["seq"], seal_row["link"]) != seal_row["link"]:
                report.rewritten.append(seal_row["seq"])
            if (
                overdue is not None
                and seal_row["seq"] > newest_anchored
                and seal_row["sealed_at"] < overdue
            ):
                report.unanchored += 1
            previous = Head(seal_row["seq"], seal_row["link"])
        report.rewritten.extend(sorted(anchored))  # Anchored seals now gone.
        report.unsealed = await _unsealed(connection, now)
    return report


type _Mark = tuple[datetime, UUID]
"""A row's place in sealing order: ``(occurred_at, id)``."""


async def _seal_batch(
    connection: AsyncConnection,
    batch: int,
    late_window: timedelta,
    after: _Mark | None,
) -> tuple[int, _Mark | None]:
    """Seal up to ``batch`` rows after the head, oldest first; where it stopped.

    The first batch of a run looks from ``late_window`` before the head's
    row; the next ones from the last row sealed (``after``), since every
    unsealed row before it was sealed by then.
    """
    head = await _head(connection)
    sealed_at = clock.now()
    unsealed = _unsealed_rows().limit(batch)
    if after is not None:
        occurred_at, entry_id = after
        place = tuple_(audit_log.c.occurred_at, audit_log.c.id)
        unsealed = unsealed.where(
            place
            > tuple_(
                literal(occurred_at, audit_log.c.occurred_at.type),
                literal(entry_id, audit_log.c.id.type),
            )
        )
    else:
        horizon = await _horizon(connection, head, sealed_at, late_window)
        if horizon is not None:
            unsealed = unsealed.where(audit_log.c.occurred_at >= horizon)
    rows = (await connection.execute(unsealed)).mappings().all()
    previous = head or Head(0, GENESIS)
    seals = []
    for row in rows:
        row_digest = digest(row)
        previous = Head(previous.seq + 1, link(previous.link, row_digest))
        seals.append(
            {
                "seq": previous.seq,
                "entry_id": row["id"],
                "digest": row_digest,
                "link": previous.link,
                "sealed_at": sealed_at,
            }
        )
    if not seals:
        return 0, after
    await connection.execute(insert(audit_seals), seals)
    return len(seals), (rows[-1]["occurred_at"], rows[-1]["id"])


def _unsealed_rows() -> Select[Any]:
    """Rows of the trail without a seal, in sealing order."""
    return (
        select(*(audit_log.c[name] for name in COLUMNS))
        .outerjoin(audit_seals, audit_seals.c.entry_id == audit_log.c.id)
        .where(audit_seals.c.entry_id.is_(None))
        .order_by(audit_log.c.occurred_at, audit_log.c.id)
    )


async def _report_backlog(database: str, late_window: timedelta, warn: int) -> None:
    """Count what a run that hit its bound left; warn past ``warn`` rows."""
    left = await unsealed_backlog(database, late_window)
    _observe_backlog(database, left)
    if left > warn:
        logger.warning(
            "Audit sealing of %r is behind: %d rows wait to be sealed. Raise "
            "AUDIT.SEAL_BATCHES or seal more often; until sealed, rows can "
            "be changed unnoticed",
            database,
            left,
        )


async def _head(connection: AsyncConnection) -> Head | None:
    """The chain's newest seal, if any."""
    found = (
        await connection.execute(
            select(audit_seals.c.seq, audit_seals.c.link)
            .order_by(audit_seals.c.seq.desc())
            .limit(1)
        )
    ).first()
    return None if found is None else Head(found.seq, found.link)


async def _horizon(
    connection: AsyncConnection,
    head: Head | None,
    now: datetime,
    late_window: timedelta,
) -> datetime | None:
    """Where to look for unsealed rows: ``late_window`` before the head's row.

    The head's row, the last one sealed, is the high-water mark. One dated
    after ``now`` (a clock running ahead) counts as ``now``, so it cannot
    leave the rows written before it unsealable.
    """
    if head is None:
        return None
    newest = await connection.scalar(
        select(audit_log.c.occurred_at)
        .join(audit_seals, audit_seals.c.entry_id == audit_log.c.id)
        .where(audit_seals.c.seq == head.seq)
    )
    return None if newest is None else min(newest, now) - late_window


async def _unsealed(connection: AsyncConnection, now: datetime) -> int:
    """Rows older than ``LOOKBACK`` that have no seal."""
    count = await connection.scalar(
        select(func.count())
        .select_from(audit_log)
        .outerjoin(audit_seals, audit_seals.c.entry_id == audit_log.c.id)
        .where(audit_seals.c.entry_id.is_(None))
        .where(audit_log.c.occurred_at < now - LOOKBACK)
    )
    return int(count or 0)


async def _anchors(database: str, ledger: Ledger) -> dict[int, str]:
    """Every anchored head of ``database``: its link by number."""
    key = anchor_key(database)
    try:
        latest = await ledger.read(key)
    except LedgerKeyNotFound:
        return {}
    anchors: dict[int, str] = {}
    for revision in range(1, latest.revision):
        written = Anchor.decode((await ledger.read(key, revision)).value)
        anchors[written.seq] = written.link
    newest = Anchor.decode(latest.value)
    anchors[newest.seq] = newest.link
    return anchors


def _plain(value: object) -> object:
    """A column value as JSON: ids and times as text, the rest as they are."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    return value


def _canonical(document: object) -> bytes:
    """``document`` as compact JSON with sorted keys.

    Strings can hold lone surrogates (JSON can escape half a surrogate
    pair); they are encoded as they are, so every row has a digest, and the
    bytes of every other string stay plain UTF-8.
    """
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8", "surrogatepass")
