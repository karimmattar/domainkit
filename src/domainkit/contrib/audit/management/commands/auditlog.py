"""``auditlog``: print recent audit entries."""

from argparse import ArgumentParser

from sqlalchemy import select

from domainkit.apps import registry
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.management import BaseCommand


class Command(BaseCommand):
    """List entries newest first, optionally for one actor or action."""

    help = "Show the audit trail."

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

    async def handle(
        self,
        *,
        actor: str | None,
        action: str | None,
        limit: int,
        database: str | None,
        **options: object,
    ) -> None:
        """Print one line per entry."""
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
