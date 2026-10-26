"""Retention that keeps the chain: prune below a signed checkpoint; export checkpoints.

``prune`` cuts a database's trail at the newest seal sealed before a date
(and, with a ledger, anchored), never one younger than
``AUDIT.MIN_RETENTION``: it checks the chain up to the cut, records a
checkpoint of it signed with ``AUDIT.CHECKPOINT_KEY`` on the ledger and
stores it, then deletes the rows and seals at and below the cut, newest
first, a batch per transaction. ``verify`` then starts from the
checkpoint, so the pruned chain still verifies, and a chain tampered with
still fails. A run cut short leaves a gap the checkpoint ends, which
verifies too; running it again finishes.

On PostgreSQL the ``protect()`` triggers refuse every delete, so each
batch disables them in its own transaction, as the tables' owner
(``MIGRATION_URL``): writers of the trail wait for that batch, a few
milliseconds. The newest seal is never pruned, so sealing goes on from it.

``export`` writes every database's stored checkpoints and one of its
current head, for a verifier who holds only the public key
(``checkpoints.check_export``). See ADR 0082.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit import clock
from domainkit.apps import registry
from domainkit.db import connections
from domainkit.db.connections import create_owner_engine
from domainkit.exceptions import DomainkitError
from domainkit.http.errors import sqlstate
from domainkit.ledger import Ledger

from .checkpoints import (
    EXPORT_FORMAT,
    Checkpoint,
    key_id,
    public_pem,
    sign,
    signed,
    signing_key,
    stored,
)
from .personal import audited_databases
from .protection import trigger_names
from .recorder import record
from .seals import (
    Report,
    _head,
    _history,
    first_anchored,
    pruned_key,
    pruned_record,
    starting_points,
    verify,
)
from .settings import AuditSettings
from .tables import audit_checkpoints, audit_log, audit_seals

__all__ = [
    "ExportRefused",
    "PruneRefused",
    "Pruned",
    "export",
    "prune",
]

BATCH: Final = 1000
"""Seals, and their rows, deleted per transaction."""
_ENABLED: Final = text(
    "SELECT tgname FROM pg_trigger WHERE tgrelid = to_regclass(:table) "
    "AND tgname = :name AND NOT tgisinternal AND tgenabled <> 'D'"
)


class PruneRefused(DomainkitError):
    """The trail cannot be pruned as asked; nothing was deleted."""


@dataclass(frozen=True, slots=True)
class Pruned:
    """What ``prune`` did to one database's trail."""

    database: str
    checkpoint: Checkpoint | None
    """The checkpoint it pruned below; None when there was nothing to prune."""
    rows: int
    """Rows of the trail deleted (their seals with them)."""


async def prune(
    database: str,
    *,
    before: datetime,
    ledger: Ledger | None = None,
    now: datetime | None = None,
) -> Pruned:
    """Prune ``database``'s trail below a checkpoint of its last seal before ``before``.

    With ``ledger`` the cut is the newest seal at or below that anchored
    ``AUDIT.MIN_RETENTION`` ago, so the ledger vouches for the checkpoint's
    link too, and records the checkpoint before anything is deleted. What a
    run cut short left below the last checkpoint is deleted first.

    Raises:
        PruneRefused: ``before`` is within ``AUDIT.MIN_RETENTION`` of
            ``now``, the chain up to the cut does not verify, a stored
            checkpoint does not, or the owner may not disable the triggers.
        ImproperlyConfigured: ``AUDIT.CHECKPOINT_KEY`` is not set or
            invalid, or ``CHECKPOINT_PUBLIC_KEY`` is not its public half.
    """
    key = signing_key()
    public = key.public_key()
    now = now or clock.now()
    config = registry.settings(AuditSettings)
    if before > now - config.MIN_RETENTION:
        raise PruneRefused(
            f"AUDIT.MIN_RETENTION keeps every row for {config.MIN_RETENTION}: "
            f"prune before {(now - config.MIN_RETENTION).isoformat()} at the "
            "latest. Nothing was pruned."
        )
    history = [] if ledger is None else await _history(database, ledger)
    async with connections.engine(database).connect() as connection:
        head = await _head(connection)
        cut = await connection.scalar(
            select(func.max(audit_seals.c.seq)).where(
                audit_seals.c.sealed_at < before,
                audit_seals.c.seq < (head.seq if head else 0),
            )
        )
        points = await stored(connection, database, public)
    accepted, forged = await starting_points(database, points, ledger, history, now=now)
    if forged:
        raise PruneRefused(
            f"Checkpoints of {database!r} at seals {forged} do not verify "
            "(auditverify); nothing was pruned."
        )
    last = accepted[points[-1].seq] if points else None
    left = 0 if last is None else await _left_below(database, last, ledger)
    if cut is not None and ledger is not None:
        oldest = now - config.MIN_RETENTION
        cut = max(
            (
                written.seq
                for written in history
                if written.seq <= cut
                and (first_anchored(history, written.seq) or now) <= oldest
            ),
            default=None,
        )
    if cut is None or (last is not None and cut <= last.seq):
        if left:
            await _record(database, last, left, before)
        return Pruned(database, last if left else None, left)
    _refuse_unless_intact(database, await verify(database, ledger, upto=cut))
    async with connections.engine(database).begin() as connection:
        sealed = (
            await connection.execute(
                select(audit_seals.c.link, audit_seals.c.sealed_at).where(
                    audit_seals.c.seq == cut
                )
            )
        ).one()
        checkpoint = Checkpoint(
            database=database,
            namespace=config.NAMESPACE,
            seq=cut,
            link=sealed.link,
            sealed_at=sealed.sealed_at,
            made_at=now,
            previous=None if last is None else (last.seq, last.link),
            key_id=key_id(public),
            kind="prune",
        )
        if ledger is not None:  # Recorded first: else it starts nothing.
            await ledger.append(
                pruned_key(database, config.NAMESPACE),
                pruned_record(checkpoint.document()),
            )
        await connection.execute(
            audit_checkpoints.insert().values(
                seq=cut,
                document=checkpoint.document(),
                signature=sign(checkpoint, key),
                made_at=now,
            )
        )
    rows = left + await _delete(database, 0 if last is None else last.seq, cut)
    await _record(database, checkpoint, rows, before)
    return Pruned(database, checkpoint, rows)


async def _record(
    database: str, checkpoint: Checkpoint | None, rows: int, before: datetime
) -> None:
    """Record a prune in the trail it pruned: below which seal, and how many rows."""
    await record(
        "audit.pruned",
        target=database,
        seq=None if checkpoint is None else checkpoint.seq,
        rows=rows,
        before=before.isoformat(),
    )


async def _left_below(
    database: str, checkpoint: Checkpoint, ledger: Ledger | None
) -> int:
    """Delete what a run cut short left at or below ``checkpoint``; count the rows.

    Checked first, as a new cut is: rows changed since would vanish unseen.
    """
    async with connections.engine(database).connect() as connection:
        found = await connection.scalar(
            select(func.count())
            .select_from(audit_seals)
            .where(audit_seals.c.seq <= checkpoint.seq)
        )
    if not found:
        return 0
    _refuse_unless_intact(database, await verify(database, ledger, upto=checkpoint.seq))
    lowest = 0 if checkpoint.previous is None else checkpoint.previous[0]
    return await _delete(database, lowest, checkpoint.seq)


def _refuse_unless_intact(database: str, report: Report) -> None:
    """Refuse to prune a chain with a problem up to the cut: pruning would hide it."""
    problems = {
        "changed rows": len(report.changed),
        "missing rows": len(report.missing),
        "broken links": len(report.broken),
        "rewritten anchors": len(report.rewritten),
    }
    if found := {name: count for name, count in problems.items() if count}:
        raise PruneRefused(
            f"The trail of {database!r} does not verify up to the cut ({found}); "
            "pruning it would hide that. Nothing was pruned: run auditverify."
        )


async def _delete(database: str, lowest: int, cut: int) -> int:
    """Delete seals above ``lowest`` up to ``cut``, and their rows, newest first."""
    postgres = connections.engine(database).dialect.name == "postgresql"
    engine = (
        create_owner_engine(connections.config(database))
        if postgres
        else connections.engine(database)
    )
    deleted = 0
    try:
        top = cut
        while top > lowest:
            bottom = max(lowest, top - BATCH)
            in_batch = (audit_seals.c.seq > bottom) & (audit_seals.c.seq <= top)
            async with engine.begin() as connection:
                disabled = await _disable_triggers(connection) if postgres else []
                rows = await connection.execute(
                    delete(audit_log).where(
                        audit_log.c.id.in_(
                            select(audit_seals.c.entry_id).where(in_batch)
                        )
                    )
                )
                await connection.execute(delete(audit_seals).where(in_batch))
                for statement in disabled:
                    await connection.execute(text(statement))
            deleted += rows.rowcount
            top = bottom
    except DBAPIError as exc:
        if sqlstate(exc) != "42501":
            raise
        raise PruneRefused(
            f"The trail of {database!r} is protected by triggers only its owner may "
            f"disable: set DATABASES[{database!r}]['MIGRATION_URL'] to the owner's "
            "URL. The checkpoint is stored; run auditprune again."
        ) from exc
    finally:
        if postgres:
            await engine.dispose()
    return deleted


async def _disable_triggers(connection: AsyncConnection) -> list[str]:
    """Disable the ``protect()`` row triggers present; the statements that enable them.

    Until the transaction ends: the ``ALTER TABLE`` locks the tables, and a
    rollback restores the triggers.
    """
    enable = []
    quote = connection.dialect.identifier_preparer.quote
    for table in (audit_log, audit_seals):
        name, _ = trigger_names(table)
        found = await connection.scalar(_ENABLED, {"table": table.name, "name": name})
        if found is None:
            continue
        # The names are the framework's own, quoted by the dialect.
        await connection.execute(
            # nosemgrep: avoid-sqlalchemy-text  # noqa: ERA001
            text(f"ALTER TABLE {quote(table.name)} DISABLE TRIGGER {quote(name)}")
        )
        enable.append(f"ALTER TABLE {quote(table.name)} ENABLE TRIGGER {quote(name)}")
    return enable


class ExportRefused(DomainkitError):
    """A trail does not verify: no checkpoint of it is signed."""


async def export(
    ledger: Ledger | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Every audited database's checkpoints, and one of its head, signed.

    Raises:
        ExportRefused: A trail does not verify (``verify``): a checkpoint
            of it would vouch for what is wrong.
        ImproperlyConfigured: ``AUDIT.CHECKPOINT_KEY`` is not set or
            invalid, or ``CHECKPOINT_PUBLIC_KEY`` is not its public half.
    """
    key = signing_key()
    public = key.public_key()
    now = now or clock.now()
    namespace = registry.settings(AuditSettings).NAMESPACE
    databases: dict[str, Any] = {}
    for database in audited_databases():
        report = await verify(database, ledger, now=now)
        if not report.intact:
            raise ExportRefused(
                f"The trail of {database!r} does not verify: run auditverify."
            )
        async with connections.engine(database).connect() as connection:
            points = await stored(connection, database, public)
            head = (
                await connection.execute(
                    select(
                        audit_seals.c.seq, audit_seals.c.link, audit_seals.c.sealed_at
                    )
                    .order_by(audit_seals.c.seq.desc())
                    .limit(1)
                )
            ).first()
        chain = [
            signed(point.checkpoint, point.signature)
            for point in points
            if point.checkpoint is not None  # All are: the trail verified.
        ]
        last = points[-1].checkpoint if points else None
        if head is not None and (last is None or head.seq > last.seq):
            newest = Checkpoint(
                database=database,
                namespace=namespace,
                seq=head.seq,
                link=head.link,
                sealed_at=head.sealed_at,
                made_at=now,
                previous=None if last is None else (last.seq, last.link),
                key_id=key_id(public),
                kind="export",
            )
            chain.append(signed(newest, sign(newest, key)))
        databases[database] = {"checkpoints": chain}
    return {
        "format": EXPORT_FORMAT,
        "exported_at": now.isoformat(),
        "namespace": namespace,
        "key_id": key_id(public),
        "public_key": public_pem(public),
        "databases": databases,
    }
