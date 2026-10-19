"""``migrate``: bring database schemas to a revision, target by target.

``--plan`` lists what would run, ``--sql`` prints it as SQL (rendered
offline by Alembic), and ``--fake`` records a revision as applied without
running it. None of them changes the schema.
"""

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
        """Accept a revision, a target, a direction, and a dry mode."""
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
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--plan", action="store_true", help="list the revisions that would run"
        )
        mode.add_argument(
            "--sql",
            action="store_true",
            help="print the SQL instead of running it (rendered offline)",
        )
        mode.add_argument(
            "--fake",
            action="store_true",
            help="record TARGET as applied without running anything",
        )
        parser.add_argument(
            "--from",
            dest="start",
            metavar="REVISION",
            help="with --sql: the revision to start from (default: the database's)",
        )

    def handle(
        self,
        *,
        target: str,
        database: str | None,
        downgrade: bool,
        allow_data_loss: bool,
        plan: bool,
        sql: bool,
        fake: bool,
        start: str | None,
        **options: object,
    ) -> None:
        """Apply, revert, plan, print, or record revisions on each database."""
        if start is not None and not sql:
            raise CommandError("--from goes with --sql.")
        if (
            downgrade
            and not (plan or sql)
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
            try:
                if plan:
                    self._plan(alias, target, downgrade=downgrade)
                elif sql:
                    migrations.sql(
                        target,
                        alias,
                        start=start,
                        downgrade=downgrade,
                        output=self.stdout,
                    )
                elif fake:
                    migrations.fake(target, alias, downgrade=downgrade)
                    self.write(
                        f"Database {alias!r} is recorded at {target}; nothing ran."
                    )
                else:
                    self._migrate(alias, target, downgrade=downgrade)
            except AlembicError as exc:
                hint = ""
                if "Multiple head" in str(exc):
                    hint = " Run 'manage.py makemigrations --merge' first."
                raise CommandError(f"{exc}{hint}") from exc

    def _plan(self, alias: str, target: str, *, downgrade: bool) -> None:
        """List what moving one database to ``target`` would run."""
        steps = migrations.plan(target, alias, downgrade=downgrade)
        if not steps:
            self.write(f"Database {alias!r}: nothing to do.")
            return
        self.write(f"Database {alias!r}:")
        for step in steps:
            action = "apply " if step.upgrade else "revert"
            self.write(f"  {action} {step.revision}  {step.message}")

    def _migrate(self, alias: str, target: str, *, downgrade: bool) -> None:
        """Move one database, then create its partitions when upgrading."""
        if downgrade:
            migrations.downgrade(target, alias)
        else:
            migrations.upgrade(target, alias)
        self.write(f"Database {alias!r} is at {target}.")
        if downgrade:
            return
        if created := migrations.create_partitions(alias):
            self.write(f"Created {len(created)} partition(s) in {alias!r}.")
        migrations.run_migrated_hooks(alias)
