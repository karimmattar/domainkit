"""``migrate``: bring database schemas to a revision, target by target."""

from argparse import ArgumentParser
from typing import Final

from alembic.util.exc import CommandError as AlembicError

from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import migrations
from domainkit.db.routing import router
from domainkit.management.base import BaseCommand, CommandError

SYMBOLIC: Final = frozenset({"head", "heads", "base"})
"""Revisions that mean the same thing in every target's history."""


class Command(BaseCommand):
    """Upgrade to ``head`` (or a revision); ``--downgrade`` moves down instead."""

    help = "Apply migrations to every target's databases (uses MIGRATION_URL when set)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a revision, a target, and a direction."""
        parser.add_argument(
            "target", nargs="?", default="head", help="revision (default: head)"
        )
        parser.add_argument(
            "--database",
            help="a target (SQL alias or shard group) or an alias; default: all",
        )
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
        database: str | None,
        downgrade: bool,
        allow_data_loss: bool,
        **options: object,
    ) -> None:
        """Apply or revert revisions on each database, then report it."""
        if (
            downgrade
            and settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS
            and not allow_data_loss
        ):
            raise CommandError(
                f"Downgrading in {settings.ENVIRONMENT} can drop data; repeat with "
                "--allow-data-loss after taking a backup."
            )
        targets = (database,) if database else migrations.migration_targets()
        if len(targets) > 1 and target not in SYMBOLIC:
            raise CommandError(
                f"Revision {target!r} belongs to one target's history; name the "
                "target with --database."
            )
        for alias in (alias for name in targets for alias in router.aliases_of(name)):
            self._migrate(alias, target, downgrade=downgrade)

    def _migrate(self, alias: str, target: str, *, downgrade: bool) -> None:
        """Move one database, then create its partitions when upgrading."""
        try:
            if downgrade:
                migrations.downgrade(target, alias)
            else:
                migrations.upgrade(target, alias)
        except AlembicError as exc:
            hint = ""
            if "Multiple head" in str(exc):
                hint = " Run 'manage.py makemigrations --merge' first."
            raise CommandError(f"{exc}{hint}") from exc
        self.write(f"Database {alias!r} is at {target}.")
        if not downgrade and (created := migrations.create_partitions(alias)):
            self.write(f"Created {len(created)} partition(s) in {alias!r}.")
