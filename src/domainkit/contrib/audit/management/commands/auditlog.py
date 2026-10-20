"""``auditlog``: print recent audit entries, or one record's history."""

from argparse import ArgumentParser

from sqlalchemy import select

from domainkit.apps import registry
from domainkit.contrib.audit.history import history
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.management import BaseCommand, CommandError


class Command(BaseCommand):
    """List entries newest first, optionally for one actor or action.

    ``--target`` and ``--aggregate FIELD=ID`` list one record's history
    from every audited database instead, with what each entry changed.
    """

    help = "Show the audit trail, or a record's history (--target, --aggregate)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept filters and a limit."""
        parser.add_argument("--actor")
        parser.add_argument("--action")
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument(
            "--database",
            help="the alias to read (default: AUDIT.DATABASE); events are audited "
            "in the database where they commit",
        )
        about = parser.add_mutually_exclusive_group()
        about.add_argument("--target", help="a record's history, by its target")
        about.add_argument(
            "--aggregate",
            metavar="FIELD=ID",
            help="an aggregate's history: rows whose details[FIELD] is ID",
        )

    async def handle(
        self,
        *,
        actor: str | None,
        action: str | None,
        limit: int,
        database: str | None,
        target: str | None,
        aggregate: str | None,
        **options: object,
    ) -> None:
        """Print one line per entry."""
        if target is not None or aggregate is not None:
            await self._history(target, aggregate, limit, database)
            return
        config = registry.settings(AuditSettings)
        statement = select(audit_log).order_by(audit_log.c.occurred_at.desc())
        if actor:
            statement = statement.where(audit_log.c.actor == actor)
        if action:
            statement = statement.where(audit_log.c.action == action)
        alias = database or config.DATABASE
        async with connections.engine(alias).connect() as connection:
            rows = (await connection.execute(statement.limit(limit))).all()
        for row in rows:
            self.write(
                f"{row.occurred_at:%Y-%m-%d %H:%M:%S}  {row.actor}  {row.action}  "
                f"{row.outcome}  {row.target or '-'}  {row.request_id or '-'}"
            )

    async def _history(
        self,
        target: str | None,
        aggregate: str | None,
        limit: int,
        database: str | None,
    ) -> None:
        """Print a record's history, each entry's changes below it."""
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
