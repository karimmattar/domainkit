"""``flush``: delete every row of a database, as ``migrate`` left it empty."""

from argparse import ArgumentParser

from domainkit.conf import DEFAULT_DB_ALIAS, settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import fixtures, migrations
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Empty every table but the kept ones, then run the ``migrated`` hooks.

    Append-only tables (the audit trail) and tables marked ``info={"flush":
    False}`` are kept. The hooks put back what ``migrate`` writes, such as
    content types and permissions. Staging and production refuse without
    ``--force``.
    """

    help = "Delete every row (but the audit trail's), then rerun the migrate hooks."

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

    async def handle(
        self, *, database: str, dry_run: bool, force: bool, **options: object
    ) -> None:
        """Say what goes, then delete it unless refused."""
        counts, kept = await fixtures.flush_plan(database)
        for table, rows in counts.items():
            self.write(f"{database}: {table}: delete {_rows(rows)}")
        for table in kept:
            self.write(f"{database}: {table}: kept")
        if dry_run:
            return
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS and not force:
            raise CommandError(
                f"Flushing deletes every row in {settings.ENVIRONMENT}; repeat with "
                "--force after taking a backup."
            )
        await fixtures.flush(database)
        migrations.run_migrated_hooks(database)
        self.write(f"Deleted {_rows(sum(counts.values()))} from {database!r}.")


def _rows(count: int) -> str:
    """``1 row``, ``2 rows``."""
    return f"{count} row{'' if count == 1 else 's'}"
