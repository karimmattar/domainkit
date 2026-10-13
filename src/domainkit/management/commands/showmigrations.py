"""``showmigrations``: list revisions and whether the database has them."""

from argparse import ArgumentParser

from domainkit.conf import DEFAULT_DB_ALIAS
from domainkit.db import migrations
from domainkit.management.base import BaseCommand


class Command(BaseCommand):
    """Print ``[X]`` for applied revisions and ``[ ]`` for pending ones."""

    help = "List migrations, oldest first, and which ones are applied."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a database alias."""
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)

    def handle(self, *, database: str, **options: object) -> None:
        """Print one line per revision."""
        revisions = migrations.status(database)
        if not revisions:
            self.write("No migrations.")
        for revision in revisions:
            mark = "X" if revision.applied else " "
            self.write(f"[{mark}] {revision.revision}  {revision.message}")
