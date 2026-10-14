"""``makemigrations``: write revisions for model changes, per target."""

from argparse import ArgumentParser

from alembic.util.exc import CommandError as AlembicError

from domainkit.db import migrations
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Compare each target's tables with its database and write a revision."""

    help = "Autogenerate migrations from the installed domains' tables."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a message, a target, and the check and merge modes."""
        parser.add_argument("-m", "--message", default="auto", help="revision name")
        parser.add_argument(
            "--database",
            help="a target (SQL alias or shard group); default: every target",
        )
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
        database: str | None,
        check: bool,
        merge: bool,
        **options: object,
    ) -> None:
        """Write revisions, merge revisions, or only report with ``--check``."""
        targets = (database,) if database else migrations.migration_targets()
        if merge:
            merged = [
                path for target in targets if (path := migrations.make_merge(target))
            ]
            self.write(
                "\n".join(f"Created {path}." for path in merged) or "Nothing to merge."
            )
            return
        for target in targets:
            if len(heads := migrations.heads(target)) > 1:
                raise CommandError(
                    f"The migration history of {target!r} has {len(heads)} heads; "
                    "run 'manage.py makemigrations --merge' first."
                )
        try:
            if check:
                self._check(targets)
                return
            created = [
                path
                for target in targets
                if (path := migrations.make_migration(message, target))
            ]
        except AlembicError as exc:
            raise CommandError(f"{exc} Run 'manage.py migrate' first.") from exc
        self.write(
            "\n".join(f"Created {path}." for path in created) or "No changes detected."
        )

    def _check(self, targets: tuple[str, ...]) -> None:
        """Fail if any target has model changes without a revision."""
        missing = {
            target: changes
            for target in targets
            if (changes := migrations.pending_changes(target))
        }
        if missing:
            raise CommandError(
                "Model changes have no migration: "
                + "; ".join(
                    f"{target}: {changes}" for target, changes in missing.items()
                )
            )
        self.write("No changes detected.")
