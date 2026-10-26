"""``flush``: delete every row of a database, as ``migrate`` left it empty."""

from argparse import ArgumentParser

from domainkit.conf import DEFAULT_DB_ALIAS, settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import fixtures, migrations
from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.operators import require_named


class Command(BaseCommand):
    """Empty every table but the kept ones, then run the ``migrated`` hooks.

    Append-only tables (the audit trail) and tables marked ``info={"flush":
    False}`` are kept. So are guarded tables (users, permissions, grants,
    tokens, second factors, ...) and the tables they reference, unless
    ``--include-guarded``. The hooks put back what ``migrate`` writes, such
    as content types and permissions. Staging and production refuse without
    ``--force``, and without an operator named by ``--operator`` or
    ``DOMAINKIT_OPERATOR``.
    """

    help = (
        "Delete every row but the audit trail's and, unless --include-guarded, "
        "the guarded tables'; then rerun the migrate hooks."
    )
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept an alias and the safety switches."""
        parser.add_argument(
            "--database", default=DEFAULT_DB_ALIAS, help="the alias to empty"
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="only say what would be deleted"
        )
        parser.add_argument(
            "--force", action="store_true", help="required in staging and production"
        )
        parser.add_argument(
            "--include-guarded",
            action="store_true",
            help="empty guarded tables too: users, permissions, grants, tokens, ...",
        )

    async def handle(
        self,
        *,
        database: str,
        dry_run: bool,
        force: bool,
        include_guarded: bool,
        **options: object,
    ) -> None:
        """Say what goes, then delete it unless refused."""
        plan = await fixtures.flush_plan(database, include_guarded=include_guarded)
        for table, rows in plan.counts.items():
            self.write(f"{database}: {table}: delete {_rows(rows)}")
        for table in plan.kept:
            self.write(f"{database}: {table}: kept")
        for table in plan.guarded:
            self.write(f"{database}: {table}: kept (guarded)")
        if dry_run:
            return
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS and not force:
            raise CommandError(
                f"Flushing deletes every row in {settings.ENVIRONMENT}; repeat with "
                "--force after taking a backup."
            )
        require_named(self.operator, "flush")
        rows = sum(plan.counts.values())
        async with self.recorded(
            "flush",
            database=database,
            rows=rows,
            tables=plan.counts,
            kept=[*plan.kept, *plan.guarded],
            include_guarded=include_guarded,
        ):
            await fixtures.flush(database, include_guarded=include_guarded)
            migrations.run_migrated_hooks(database)
        self.write(f"Deleted {_rows(rows)} from {database!r}.")


def _rows(count: int) -> str:
    """``1 row``, ``2 rows``."""
    return f"{count} row{'' if count == 1 else 's'}"
