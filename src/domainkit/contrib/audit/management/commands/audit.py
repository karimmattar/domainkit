"""``audit``: read, seal, verify, prune, and forget the audit trail.

``manage.py audit <action>``:

- ``log``: recent entries, newest first, by actor or action;
- ``history``: one record's history (``--target``, or ``--aggregate FIELD=ID``),
  each entry's changes below it;
- ``seal``: seal new rows into each database's hash chain, and anchor its
  head on ``AUDIT.LEDGER`` (the worker does it every ``AUDIT.SEAL_EVERY``);
- ``verify``: check the rows against their seals, links, and anchors, and
  with ``--export FILE`` write signed checkpoints for an outside verifier;
- ``check-export``: check such a file offline, with only the public key;
- ``prune``: delete rows sealed before ``--keep-days``, below a signed
  checkpoint;
- ``forget``: make subjects' pseudonyms unlinkable to them.

``prune`` and ``forget`` change what is stored: each run is recorded in the
trail as the operator who ran it (``operator.auditprune``,
``operator.auditforget``), with counts, never the subjects.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from domainkit import clock
from domainkit.apps import registry
from domainkit.contrib.audit.checkpoints import CheckpointInvalid, check_export
from domainkit.contrib.audit.history import history
from domainkit.contrib.audit.personal import (
    audited_databases,
    forget,
    forget_anonymous,
)
from domainkit.contrib.audit.pruning import ExportRefused, PruneRefused, export, prune
from domainkit.contrib.audit.seals import Report, seal_all, verify
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.audit.trails import in_trail, trails
from domainkit.db import connections
from domainkit.ledger import Ledger, ledgers
from domainkit.management import ActionCommand, CommandError, action, option


class Command(ActionCommand):
    """The audit trail's actions; ``prune`` and ``forget`` run as an operator."""

    help = "Read, seal, verify, prune, or forget the audit trail."

    @action(
        help="show recent entries, newest first",
        options=[
            option("--actor", help="only this actor's"),
            option("--action", dest="named", help="only this action's"),
            option(
                "--limit", type=int, default=50, help="at most this many (default: 50)"
            ),
            option(
                "--database",
                help="the alias to read (default: AUDIT.DATABASE); events are "
                "audited in the database where they commit",
            ),
        ],
    )
    async def log(
        self, *, actor: str | None, named: str | None, limit: int, database: str | None
    ) -> None:
        """Print one line per entry."""
        config = registry.settings(AuditSettings)
        statement = select(audit_log).order_by(audit_log.c.occurred_at.desc())
        if actor:
            statement = statement.where(audit_log.c.actor == actor)
        if named:
            statement = statement.where(audit_log.c.action == named)
        async with connections.engine(
            database or config.DATABASE
        ).connect() as connection:
            rows = (await connection.execute(statement.limit(limit))).all()
        for row in rows:
            self.write(
                f"{row.occurred_at:%Y-%m-%d %H:%M:%S}  {row.actor}  {row.action}  "
                f"{row.outcome}  {row.target or '-'}  {row.request_id or '-'}"
            )

    @action(
        help="show one record's history, with what each entry changed",
        options=[
            option("--target", help="the record, by its target"),
            option(
                "--aggregate",
                metavar="FIELD=ID",
                help="an aggregate: the rows whose details[FIELD] is ID",
            ),
            option(
                "--limit", type=int, default=50, help="at most this many (default: 50)"
            ),
            option("--database", help="the alias to read (default: every audited one)"),
        ],
    )
    async def history(
        self,
        *,
        target: str | None,
        aggregate: str | None,
        limit: int,
        database: str | None,
    ) -> None:
        """Print a record's history, each entry's changes below it.

        Raises:
            CommandError: Neither or both of ``--target`` and ``--aggregate``,
                or an ``--aggregate`` that is not ``FIELD=ID``.
        """
        if (target is None) == (aggregate is None):
            raise CommandError("Name the record: --target, or --aggregate FIELD=ID.")
        pair = None
        if aggregate is not None:
            field, equals, identifier = aggregate.partition("=")
            if not equals or not identifier:
                raise CommandError("--aggregate takes FIELD=ID, as product_id=42.")
            pair = (field, identifier)
        try:
            entries = await history(
                target=target,
                aggregate=pair,
                limit=limit,
                databases=None if database is None else [database],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        for entry in entries:
            self.write(
                f"{entry.occurred_at:%Y-%m-%d %H:%M:%S}  {entry.actor}  "
                f"{entry.action}  {entry.outcome}  {entry.request_id or '-'}"
            )
            for change in entry.changes:
                self.write(f"    {change.field}: {change.before!r} -> {change.after!r}")

    @action(
        help="seal new rows into each hash chain, and anchor its head on AUDIT.LEDGER",
        options=[
            option(
                "--late",
                action="store_true",
                help="also seal rows that committed later than "
                "AUDIT.SEAL_LATE_WINDOW, flagged as sealed late "
                "(reads the whole trail)",
            )
        ],
    )
    async def seal(self, *, late: bool) -> None:
        """Print what each database got."""
        for database, (sealed, anchored) in (await seal_all(late=late)).items():
            line = f"{database}: sealed {sealed} row{'' if sealed == 1 else 's'}"
            if anchored is not None:
                line += f", anchored #{anchored.seq}"
            self.write(f"{line}.")

    @action(
        help="check the rows against their seals, links, and anchors",
        options=[
            option(
                "--database", help="the alias to check (default: every audited one)"
            ),
            option(
                "--export",
                dest="export_to",
                type=Path,
                metavar="FILE",
                help="then write signed checkpoints of every trail to FILE, a new "
                "file only its owner can read (needs AUDIT.CHECKPOINT_KEY)",
            ),
        ],
    )
    async def verify(self, *, database: str | None, export_to: Path | None) -> None:
        """Print a line per trail, and the problems found.

        A trail per database, and per tenant schema of a database that
        keeps one per tenant (``trails``).

        Raises:
            CommandError: A trail failed verification, or ``--export`` was
                given with one database.
        """
        if export_to is not None and database is not None:
            raise CommandError("--export covers every audited database.")
        ledger = _ledger()
        failed = []
        aliases = (database,) if database else audited_databases()
        for alias, schema in await trails(aliases):
            with in_trail(schema):
                report = await verify(alias, ledger)
            self.write(_summary(report))
            if not report.intact:
                failed.append(report.database)
        if failed:
            raise CommandError(f"The audit trail failed verification: {failed}.")
        if export_to is not None:
            await self._export(export_to, ledger)

    @action(
        help="check an export offline, with nothing but the public key",
        options=[
            option("file", type=Path, help="the export"),
            option(
                "--public-key",
                type=Path,
                metavar="PEM",
                help="the Ed25519 public key that signed it (required)",
            ),
        ],
    )
    def check_export(self, *, file: Path, public_key: Path | None) -> None:
        """Print each database's checkpoints.

        Raises:
            CommandError: No ``--public-key``, or the file does not verify.
        """
        if public_key is None:
            raise CommandError("check-export needs --public-key.")
        try:
            checked = check_export(file.read_bytes(), public_key.read_bytes())
        except CheckpointInvalid as exc:
            raise CommandError(f"{file} does not verify: {exc}") from None
        for database, chain in checked.items():
            newest = f", newest at seal #{chain[-1].seq}" if chain else ""
            self.write(
                f"{database}: {len(chain)} checkpoint{'' if len(chain) == 1 else 's'}"
                f" verified{newest}."
            )

    @action(
        help="delete rows sealed more than --keep-days ago, below a signed "
        "checkpoint verify then starts from (on PostgreSQL, as the tables' owner)",
        options=[
            option(
                "--keep-days",
                type=int,
                required=True,
                metavar="DAYS",
                help="keep the rows sealed in the last DAYS days "
                "(at least AUDIT.MIN_RETENTION)",
            ),
            option(
                "--database", help="the alias to prune (default: every audited one)"
            ),
        ],
        operator=True,
    )
    async def prune(self, *, keep_days: int, database: str | None) -> None:
        """Prune, and print what each database lost.

        Raises:
            CommandError: ``--keep-days`` below 1, or the pruning is refused.
        """
        if keep_days < 1:
            raise CommandError("--keep-days is at least 1.")
        ledger = _ledger()
        before = clock.now() - timedelta(days=keep_days)
        async with self.recorded("auditprune", keep_days=keep_days) as record:
            record["rows"] = 0
            for alias in (database,) if database else audited_databases():
                try:
                    pruned = await prune(alias, before=before, ledger=ledger)
                except PruneRefused as exc:
                    raise CommandError(str(exc)) from None
                record["rows"] = int(str(record["rows"])) + pruned.rows
                if pruned.checkpoint is None:
                    self.write(f"{alias}: nothing to prune.")
                else:
                    self.write(
                        f"{alias}: pruned {pruned.rows} "
                        f"row{'' if pruned.rows == 1 else 's'} below seal "
                        f"#{pruned.checkpoint.seq}."
                    )

    @action(
        help="forget subjects (user ids) in every audited database: their "
        "pseudonyms can no longer be linked to them",
        options=[
            option("subjects", nargs="*", metavar="subject"),
            option(
                "--anonymous-days",
                type=int,
                metavar="DAYS",
                help="keep anonymous callers' addresses for DAYS days (UTC) and "
                "forget older ones",
            ),
        ],
        operator=True,
    )
    async def forget(self, *, subjects: list[str], anonymous_days: int | None) -> None:
        """Delete the keys and say how many there were.

        Raises:
            CommandError: Nothing to forget, or a negative ``--anonymous-days``.
        """
        if not subjects and anonymous_days is None:
            raise CommandError("Name subjects to forget, or --anonymous-days.")
        if anonymous_days is not None and anonymous_days < 0:
            raise CommandError("--anonymous-days cannot be negative.")
        async with self.recorded(
            "auditforget", subjects=len(subjects), anonymous_days=anonymous_days
        ) as record:
            forgotten = await forget(*subjects)
            if anonymous_days is not None:
                before = clock.today() - timedelta(days=anonymous_days)
                forgotten += await forget_anonymous(before)
            record["keys"] = forgotten
        self.write(f"Forgot {forgotten} key{'' if forgotten == 1 else 's'}.")

    async def _export(self, path: Path, ledger: Ledger | None) -> None:
        """Write the export to ``path``, a new file only its owner can read.

        Raises:
            CommandError: The export is refused, or ``path`` exists.
        """
        try:
            document = await export(ledger)
        except ExportRefused as exc:
            raise CommandError(str(exc)) from None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CommandError(f"{path} exists; name a new file.") from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(document, indent=2) + "\n")
        count = sum(
            len(found["checkpoints"]) for found in document["databases"].values()
        )
        self.write(
            f"Wrote {count} signed checkpoint{'' if count == 1 else 's'} to {path} "
            f"(key {document['key_id']})."
        )


def _ledger() -> Ledger | None:
    """``AUDIT.LEDGER``'s ledger, if one is set."""
    config = registry.settings(AuditSettings)
    return ledgers[config.LEDGER] if config.LEDGER is not None else None


def _summary(report: Report) -> str:
    """One line on the report, then one per kind of problem."""
    lines = [
        f"{report.database}: {report.sealed} seals, {report.anchors} anchors: "
        + ("intact." if report.intact else "NOT INTACT.")
    ]
    for name, found in (
        ("changed rows", report.changed),
        ("missing rows", report.missing),
        ("broken links at", report.broken),
        ("rewritten anchors at", report.rewritten),
        ("checkpoints that do not verify at", report.forged),
    ):
        if found:
            lines.append(f"  {name}: {', '.join(str(each) for each in found)}")
    if report.unanchored:
        lines.append(f"  seals left unanchored: {report.unanchored}")
    if report.unsealed:
        lines.append(f"  unsealed rows: {report.unsealed}")
    if report.late:
        lines.append(f"  rows sealed late (open to change for longer): {report.late}")
    if report.pruned:
        lines.append(f"  seals pruned below checkpoints: {report.pruned}")
    return "\n".join(lines)
