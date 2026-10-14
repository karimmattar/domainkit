"""``partitions``: keep partitioned tables' partitions coming, prune old ones."""

from argparse import ArgumentParser

from domainkit.db import migrations
from domainkit.db.partitioning import maintain
from domainkit.db.routing import router
from domainkit.management.base import BaseCommand


class Command(BaseCommand):
    """Create the current and upcoming partitions; drop expired ones with --prune.

    Schedule it (cron, a Kubernetes CronJob) at least once per partition
    period: inserts fail once the last partition is in the past.
    """

    help = "Create upcoming partitions; with --prune, drop those past retention."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a target, and the prune and dry-run switches."""
        parser.add_argument(
            "--database",
            help="a target (SQL alias or shard group) or an alias; default: all",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="detach and drop partitions older than each table's retain periods",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="print the SQL instead of running it",
        )

    async def handle(
        self,
        *,
        database: str | None,
        prune: bool,
        dry_run: bool,
        **options: object,
    ) -> None:
        """Maintain every database of the chosen targets and report each."""
        targets = (database,) if database else migrations.migration_targets()
        for alias in (alias for name in targets for alias in router.aliases_of(name)):
            statements = await maintain(alias, prune=prune, dry_run=dry_run)
            if dry_run:
                for statement in statements:
                    self.write(f"{statement};")
            elif statements:
                self.write(f"Partitions of {alias!r}: {len(statements)} change(s).")
            else:
                self.write(f"Partitions of {alias!r} are up to date.")
