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
unsealed. A chain pruned below a signed checkpoint (``checkpoints``,
``pruning``) starts from it instead of the genesis; a checkpoint whose
signature does not verify is reported, and starts nothing.

Rows are sealed after they commit, by the worker every ``AUDIT.SEAL_EVERY``
or by ``auditseal``, so a row can be rewritten unnoticed until it is sealed
and its head anchored. The newest sealed row is the high-water mark: a run
looks for unsealed rows from ``AUDIT.SEAL_LATE_WINDOW`` before it, for rows
that committed late, then on from the last row it sealed, so a run costs
what is new plus that window, however long the trail. The mark counts as
the database's present at most (PostgreSQL's clock, not the sealer's
host's), so a sealer whose clock runs ahead cannot move it past the rows
other hosts write. A run seals ``AUDIT.SEAL_BATCHES`` batches at most;
what is left waits for the next run, and is counted
(``audit_unsealed_rows``) and logged when it passes
``AUDIT.SEAL_BACKLOG_WARNING``.

A row that committed later than the window is out of a run's reach, and
``verify`` reports it once it is ``LOOKBACK`` old. ``seal(late=True)``
(``auditseal --late``) seals such rows too, into the same chain, each
seal flagged ``late``; finding them reads the whole trail, so it runs only
when asked. Late seals never move the high-water mark back.
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
from domainkit.db import UTCDateTime, connections
from domainkit.ledger import Ledger, LedgerChanged, LedgerKeyNotFound, ledgers

from .checkpoints import Checkpoint, configured_public_key, stored
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
_LOCK: Final = text(
    "SELECT pg_try_advisory_xact_lock(hashtext('domainkit.audit.seal'))"
)
"""One sealer per database: each batch's transaction takes it or gives up.

A sealer that finds it taken leaves the work to the one holding it instead
of queueing behind it (PERF-09). Transaction-scoped, so it ends with its
batch whatever happens to the connection, and works through PgBouncer.
"""


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
    late: int = 0
    """Seals of rows sealed out of the late window (``auditseal --late``).

    Not a problem by itself: those rows were open to change unnoticed for
    longer than the others.
    """
    pruned: int = 0
    """Seals pruned below checkpoints that verified (``auditprune``)."""
    forged: list[int] = field(default_factory=list)
    """Checkpoints, by seal, whose signature does not verify under
    ``AUDIT.CHECKPOINT_KEY`` (or no key is set): they start nothing."""

    @property
    def intact(self) -> bool:
        """Whether nothing was found wrong."""
        problems = (
            self.changed,
            self.missing,
            self.broken,
            self.rewritten,
            self.forged,
        )
        return not (any(problems) or self.unanchored or self.unsealed)


def digest(row: Mapping[Any, Any]) -> str:
    """The SHA-256 of the row's canonical JSON."""
    return hashlib.sha256(
        _canonical({name: _plain(row[name]) for name in COLUMNS})
    ).hexdigest()


def link(previous: str, row_digest: str) -> str:
    """The link after ``previous`` for a row with ``row_digest``."""
    return hashlib.sha256(f"{previous}{row_digest}".encode()).hexdigest()


def anchor_key(database: str, namespace: str | None = None) -> bytes:
    """The ledger key holding ``database``'s anchors, one revision each.

    ``domainkit/audit/<namespace>/<database>``, so projects and environments
    sharing a ledger keep their anchors apart; without a namespace, the key
    of 0.9.0 and before, ``domainkit/audit/<database>``.
    """
    if namespace is None:
        return f"domainkit/audit/{database}".encode()
    return f"domainkit/audit/{namespace}/{database}".encode()


def anchor_keys(database: str) -> tuple[bytes, ...]:
    """The keys ``verify`` reads ``database``'s anchors from, oldest first.

    The key of ``AUDIT.NAMESPACE``, after the key without a namespace while
    ``AUDIT.PREVIOUS_ANCHORS`` keeps the heads anchored there before the
    namespace was set.
    """
    config = registry.settings(AuditSettings)
    current = anchor_key(database, config.NAMESPACE)
    if config.NAMESPACE is None or not config.PREVIOUS_ANCHORS:
        return (current,)
    return (anchor_key(database), current)


async def seal(
    database: str,
    *,
    batch: int = BATCH,
    batches: int | None = None,
    late_window: timedelta | None = None,
    late: bool = False,
) -> int:
    """Seal the rows of ``database``'s trail not sealed yet; count them.

    At most ``batches`` transactions of ``batch`` rows (default:
    ``AUDIT.SEAL_BATCHES``); late rows are looked for ``late_window``
    before the newest sealed row (default: ``AUDIT.SEAL_LATE_WINDOW``).
    With ``late``, rows from before that window are sealed afterwards
    too, flagged late, in at most ``batches`` more transactions: finding
    them reads the whole trail.

    On PostgreSQL, while another sealer of the database seals a batch,
    this one stops, logs it, and leaves that sealer the rest (counting
    what it sealed before), instead of waiting for it. It still counts
    and reports what is left, and warns once the oldest row left has
    waited longer than ``SEAL_EVERY`` and the late window together: a
    lock held that long is not a sealer at work (SEC-a-1).
    """
    config = registry.settings(AuditSettings)
    batches = config.SEAL_BATCHES if batches is None else batches
    late_window = config.SEAL_LATE_WINDOW if late_window is None else late_window
    sealed, caught_up = await _seal_pass(database, batch, batches, late_window)
    if caught_up:
        _observe_backlog(database, 0)
    else:
        gave_way = caught_up is None
        await _report_backlog(
            database, late_window, config.SEAL_BACKLOG_WARNING, gave_way=gave_way
        )
        if gave_way:
            await _report_stalled(database, late_window, config.SEAL_EVERY)
    if late:
        more, _ = await _seal_pass(database, batch, batches, late_window, late=True)
        sealed += more
    return sealed


async def unsealed_backlog(database: str, late_window: timedelta) -> int:
    """Rows sealing would still seal now: unsealed, within reach of the window.

    Counted up to ``BACKLOG_COUNT_LIMIT``.
    """
    async with connections.engine(database).connect() as connection:
        waiting = _unsealed_rows().with_only_columns(audit_log.c.id)
        now = await _database_now(connection)
        horizon = await _horizon(connection, now, late_window)
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
    The write lands only if no anchor came after the one read here: another
    sealer, or a write that outlived a caller who gave up on it, may have
    anchored in between, and then there is nothing new.
    """
    now = now or clock.now()
    async with connections.engine(database).connect() as connection:
        head = await _head(connection)
    if head is None:
        return None
    key = anchor_key(database, registry.settings(AuditSettings).NAMESPACE)
    try:
        entry = await ledger.read(key)
    except LedgerKeyNotFound:
        latest, read_at = None, 0
    else:
        latest, read_at = Anchor.decode(entry.value), entry.transaction
    if latest is not None and (
        latest.seq == head.seq or timedelta(0) <= now - latest.anchored_at < every
    ):
        return None
    written = Anchor(head.seq, head.link, now)
    try:
        await ledger.append(key, written.encode(), unchanged_since=read_at)
    except LedgerChanged:
        return None
    return written


async def seal_all(*, late: bool = False) -> dict[str, tuple[int, Anchor | None]]:
    """Seal, then anchor, every audited database; what each one got.

    First it makes the keys of today's and tomorrow's anonymous callers, so
    at midnight no audited request waits for another's insert of the day's
    key (see ``prepare_anonymous_keys``). ``late`` also seals the rows out
    of the late window (see ``seal``).
    """
    config = registry.settings(AuditSettings)
    done: dict[str, tuple[int, Anchor | None]] = {}
    for database in audited_databases():
        await prepare_anonymous_keys(database)
        sealed = await seal(database, late=late)
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
    upto: int | None = None,
) -> Report:
    """Check ``database``'s chain against its rows, and against ``ledger``.

    With a ledger, seals sealed more than ``every`` (default:
    ``AUDIT.ANCHOR_EVERY``) and ``LOOKBACK`` ago must be anchored.

    A gap in the chain is accepted only where a checkpoint that verifies
    ends it: the seal after the gap must follow the checkpoint's link.
    Anchors of pruned seals are not checked, except one at a checkpoint,
    which must be its link. ``upto`` stops at that seal (for ``auditprune``:
    nothing after it is read, nor counted unsealed or unanchored).
    """
    now = now or clock.now()
    report = Report(database)
    anchored = await _anchors(database, ledger) if ledger is not None else {}
    report.anchors = len(anchored)
    newest_anchored = max(anchored, default=0)
    overdue = None  # Sealed before this, a seal must be anchored.
    if ledger is not None and upto is None:
        if every is None:
            every = registry.settings(AuditSettings).ANCHOR_EVERY
        overdue = now - every - LOOKBACK
    public_key = configured_public_key()
    previous = Head(0, GENESIS)
    query = (
        select(audit_seals, *(audit_log.c[name] for name in COLUMNS))
        .outerjoin(audit_log, audit_log.c.id == audit_seals.c.entry_id)
        .order_by(audit_seals.c.seq)
    )
    if upto is not None:
        query = query.where(audit_seals.c.seq <= upto)
    async with connections.engine(database).connect() as connection:
        points = await stored(connection, database, public_key)
        report.forged = [point.seq for point in points if point.checkpoint is None]
        checkpoints = {
            point.seq: point.checkpoint for point in points if point.checkpoint
        }
        async for row in await connection.stream(query):
            seal_row = row._mapping
            report.sealed += 1
            start = checkpoints.get(seal_row["seq"] - 1)
            if seal_row["seq"] > previous.seq + 1 and start is not None:
                report.pruned += seal_row["seq"] - 1 - previous.seq
                previous = Head(start.seq, start.link)
            if (
                seal_row["seq"] != previous.seq + 1
                or seal_row["link"] != link(previous.link, seal_row["digest"])
                or _off_checkpoint(checkpoints.get(seal_row["seq"]), seal_row["link"])
            ):
                report.broken.append(seal_row["seq"])
            report.late += seal_row["late"]
            if seal_row["id"] is None:
                report.missing.append(seal_row["entry_id"])
            elif digest(seal_row) != seal_row["digest"]:
                report.changed.append(seal_row["entry_id"])
            if anchored.pop(seal_row["seq"], {seal_row["link"]}) != {seal_row["link"]}:
                report.rewritten.append(seal_row["seq"])
            if (
                overdue is not None
                and seal_row["seq"] > newest_anchored
                and seal_row["sealed_at"] < overdue
            ):
                report.unanchored += 1
            previous = Head(seal_row["seq"], seal_row["link"])
        newest = max(checkpoints, default=0)
        for seq in sorted(anchored):  # Anchored seals now gone.
            point = checkpoints.get(seq)
            mismatch = point is not None and anchored[seq] != {point.link}
            if (seq > newest or mismatch) and (upto is None or seq <= upto):
                report.rewritten.append(seq)
        if upto is None:
            report.unsealed = await _unsealed(connection, now)
    return report


def _off_checkpoint(checkpoint: Checkpoint | None, seal_link: str) -> bool:
    """Whether a checkpoint at a seal still in the chain names another link."""
    return checkpoint is not None and checkpoint.link != seal_link


type _Mark = tuple[datetime, UUID]
"""A row's place in sealing order: ``(occurred_at, id)``."""


async def _seal_pass(
    database: str,
    batch: int,
    batches: int,
    late_window: timedelta,
    *,
    late: bool = False,
) -> tuple[int, bool | None]:
    """Seal up to ``batches`` batches; count them, and say whether it caught up.

    The horizon is taken in the first batch. A pass of rows sealed in time
    looks from it on; a ``late`` pass, below it (none without a horizon:
    then every row is within reach). None instead of whether it caught
    up: another sealer holds the lock, and this pass left it the rest.
    """
    sealed = 0
    after: _Mark | None = None
    horizon: datetime | None = None
    for number in range(batches):
        async with connections.engine(database).begin() as connection:
            if connection.dialect.name == "postgresql" and not await connection.scalar(
                _LOCK
            ):
                logger.info(
                    "Another sealer is sealing %r; this one leaves it the rest "
                    "after sealing %d row(s)",
                    database,
                    sealed,
                )
                return sealed, None
            sealed_at = await _database_now(connection)
            if number == 0:
                horizon = await _horizon(connection, sealed_at, late_window)
                if late and horizon is None:
                    return 0, True
            rows, after = await _seal_batch(
                connection, batch, sealed_at, after, horizon, late=late
            )
        sealed += rows
        if rows < batch:
            return sealed, True
    return sealed, False


async def _seal_batch(
    connection: AsyncConnection,
    batch: int,
    sealed_at: datetime,
    after: _Mark | None,
    horizon: datetime | None,
    *,
    late: bool,
) -> tuple[int, _Mark | None]:
    """Seal up to ``batch`` rows after the head, oldest first; where it stopped.

    The first batch of a pass looks from the ``horizon`` (``late_window``
    before the high-water mark) on; the next ones from the last row sealed
    (``after``), since every unsealed row before it was sealed by then. A
    ``late`` pass looks below the horizon only, and flags its seals.
    """
    head = await _head(connection)
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
    if horizon is not None:
        if late:
            unsealed = unsealed.where(audit_log.c.occurred_at < horizon)
        elif after is None:
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
                "late": late,
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


async def _report_backlog(
    database: str, late_window: timedelta, warn: int, *, gave_way: bool
) -> None:
    """Count what a run left (at its bound, or given way); warn past ``warn`` rows."""
    left = await unsealed_backlog(database, late_window)
    _observe_backlog(database, left)
    if left > warn:
        logger.warning(
            "Audit sealing of %r is behind: %d rows wait to be sealed. %s; "
            "until sealed, rows can be changed unnoticed",
            database,
            left,
            _HELD_ADVICE if gave_way else "Raise AUDIT.SEAL_BATCHES or seal more often",
        )


_HELD_ADVICE: Final = (
    "Another session holds the sealing lock (an advisory lock on "
    "hashtext('domainkit.audit.seal'), in pg_locks); end it if no sealer runs"
)


async def _report_stalled(
    database: str, late_window: timedelta, seal_every: timedelta
) -> None:
    """Warn when a run gave way and the oldest row left waited too long.

    A sealer at work seals a row within ``seal_every`` of its commit; one
    left longer than that plus ``late_window`` means the lock has been
    held across runs, by something that does not seal.
    """
    async with connections.engine(database).connect() as connection:
        now = await _database_now(connection)
        horizon = await _horizon(connection, now, late_window)
        oldest = _unsealed_rows().with_only_columns(audit_log.c.occurred_at).limit(1)
        if horizon is not None:
            oldest = oldest.where(audit_log.c.occurred_at >= horizon)
        found = await connection.scalar(oldest)
    if found is not None and now - found > seal_every + late_window:
        logger.warning(
            "Audit sealing of %r has not advanced for %d seconds: its oldest "
            "unsealed row is that old. %s; until sealed, rows can be changed "
            "unnoticed",
            database,
            int((now - found).total_seconds()),
            _HELD_ADVICE,
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
    connection: AsyncConnection, now: datetime, late_window: timedelta
) -> datetime | None:
    """Where to look for unsealed rows: ``late_window`` before the mark.

    The high-water mark is the row of the newest seal not sealed late: the
    last one sealed in time. One dated after ``now``, the database's
    present (a clock running ahead), counts as ``now``, so it cannot leave
    the rows written before it out of reach. None, without a mark (no
    seals, or that row is gone): every unsealed row is within reach.
    """
    mark = (
        select(audit_seals.c.entry_id)
        .where(audit_seals.c.late.is_(False))
        .order_by(audit_seals.c.seq.desc())
        .limit(1)
        .scalar_subquery()
    )
    newest = await connection.scalar(
        select(audit_log.c.occurred_at).where(audit_log.c.id == mark)
    )
    return None if newest is None else min(newest, now) - late_window


async def _database_now(connection: AsyncConnection) -> datetime:
    """The present on the trail's database: PostgreSQL's clock, or the host's.

    Every sealer then caps the high-water mark, and dates its seals, by the
    same clock, whatever its host's says. SQLite lives in the sealer's
    process, so its clock is the host's anyway. A frozen clock
    (``freeze_time``) decides on every database, as for the outbox
    (``domainkit.contrib.messaging.dbclock``), so tests can move time.
    """
    if connection.dialect.name == "postgresql" and clock.frozen() is None:
        moment = select(func.statement_timestamp(type_=UTCDateTime()))
        return (await connection.execute(moment)).scalar_one()
    return clock.now()


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


async def _anchors(database: str, ledger: Ledger) -> dict[int, set[str]]:
    """Every anchored head of ``database``: the links anchored for each number.

    Read from every key in ``anchor_keys``. A number anchored with two
    links (under two keys) cannot match the chain at both.
    """
    anchors: dict[int, set[str]] = {}
    for key in anchor_keys(database):
        try:
            latest = await ledger.read(key)
        except LedgerKeyNotFound:
            continue
        for revision in range(1, latest.revision):
            written = Anchor.decode((await ledger.read(key, revision)).value)
            anchors.setdefault(written.seq, set()).add(written.link)
        newest = Anchor.decode(latest.value)
        anchors.setdefault(newest.seq, set()).add(newest.link)
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
