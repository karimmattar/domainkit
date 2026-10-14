"""``showmigrations``: list revisions and whether each database has them."""

from argparse import ArgumentParser

from domainkit.conf import DEFAULT_DB_ALIAS
from domainkit.db import migrations
from domainkit.db.routing import router
from domainkit.management.base import BaseCommand


class Command(BaseCommand):
    """Print ``[X]`` for applied revisions and ``[ ]`` for pending ones."""

    help = "List migrations, oldest first, and which ones are applied."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a target or an alias."""
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="a target or alias; a shard group lists each of its databases",
        )

    def handle(self, *, database: str, **options: object) -> None:
        """Print one line per revision (per database, for a shard group)."""
        aliases = router.aliases_of(database)
        for alias in aliases:
            if len(aliases) > 1 or alias != database:
                self.write(f"{alias}:")
            revisions = migrations.status(alias)
            if not revisions:
                self.write("No migrations.")
            for revision in revisions:
                mark = "X" if revision.applied else " "
                self.write(f"[{mark}] {revision.revision}  {revision.message}")
