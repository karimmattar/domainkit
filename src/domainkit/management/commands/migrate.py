"""``migrate``: bring a database schema to a revision."""

from argparse import ArgumentParser

from alembic.util.exc import CommandError as AlembicError

from domainkit.conf import DEFAULT_DB_ALIAS, settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import migrations
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Upgrade to ``head`` (or a revision); ``--downgrade`` moves down instead."""

    help = "Apply migrations to a database (uses MIGRATION_URL when set)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a target revision, a database alias, and a direction."""
        parser.add_argument(
            "target", nargs="?", default="head", help="revision (default: head)"
        )
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument(
            "--downgrade",
            action="store_true",
            help="revert down to TARGET (use 'base' to revert everything)",
        )
        parser.add_argument(
            "--allow-data-loss",
            action="store_true",
            help="required to downgrade in staging and production",
        )

    def handle(
        self,
        *,
        target: str,
        database: str,
        downgrade: bool,
        allow_data_loss: bool,
        **options: object,
    ) -> None:
        """Apply or revert revisions, then report the target."""
        if (
            downgrade
            and settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS
            and not allow_data_loss
        ):
            raise CommandError(
                f"Downgrading in {settings.ENVIRONMENT} can drop data; repeat with "
                "--allow-data-loss after taking a backup."
            )
        try:
            if downgrade:
                migrations.downgrade(target, database)
            else:
                migrations.upgrade(target, database)
        except AlembicError as exc:
            hint = ""
            if "Multiple head" in str(exc):
                hint = " Run 'manage.py makemigrations --merge' first."
            raise CommandError(f"{exc}{hint}") from exc
        self.write(f"Database {database!r} is at {target}.")
