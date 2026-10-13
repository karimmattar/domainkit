"""``makemigrations``: write a revision for model changes."""

from argparse import ArgumentParser

from alembic.util.exc import CommandError as AlembicError

from domainkit.conf import DEFAULT_DB_ALIAS
from domainkit.db import migrations
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Compare the tables of every domain with the database and write a revision."""

    help = "Autogenerate a migration from the installed domains' tables."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a message, a database alias, and the check and merge modes."""
        parser.add_argument("-m", "--message", default="auto", help="revision name")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--check",
            action="store_true",
            help="exit with an error if a migration is missing; write nothing",
        )
        mode.add_argument(
            "--merge",
            action="store_true",
            help="join the heads that two branches left behind",
        )

    def handle(
        self,
        *,
        message: str,
        database: str,
        check: bool,
        merge: bool,
        **options: object,
    ) -> None:
        """Write the revision, a merge revision, or only report with ``--check``."""
        if merge:
            path = migrations.make_merge(database)
            self.write(f"Created {path}." if path else "Nothing to merge.")
            return
        if len(heads := migrations.heads(database)) > 1:
            raise CommandError(
                f"The migration history has {len(heads)} heads; run "
                "'manage.py makemigrations --merge' first."
            )
        try:
            if check:
                if changes := migrations.pending_changes(database):
                    raise CommandError(f"Model changes have no migration: {changes}")
                self.write("No changes detected.")
                return
            path = migrations.make_migration(message, database)
        except AlembicError as exc:
            raise CommandError(f"{exc} Run 'manage.py migrate' first.") from exc
        self.write(f"Created {path}." if path else "No changes detected.")
