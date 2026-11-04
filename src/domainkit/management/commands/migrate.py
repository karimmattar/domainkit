"""``migrate``: bring database schemas to a revision, target by target.

``--plan`` lists what would run, ``--sql`` prints it as SQL (rendered
offline by Alembic), and ``--fake`` records a revision as applied without
running it. None of them changes the schema.

A downgrade can drop data: staging and production refuse one without
``--allow-data-loss``, a tenant's schema included, and each one that runs
is recorded in the audit trail (``operator.migrate.downgrade``) before it
runs, so a downgrade that drops the trail's own tables is still on record.
"""

from argparse import ArgumentParser
from typing import Final

from alembic.util.exc import CommandError as AlembicError

from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import migrations
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import BaseCommand, CommandError

SYMBOLIC: Final = frozenset({"head", "heads", "base"})
"""Revisions that mean the same thing in every target's history."""


def unreachable(alias: str, exc: migrations.DatabaseUnreachable) -> CommandError:
    """The one-line error of a command that could not connect to ``alias``."""
    return CommandError(
        f"Database {alias!r} {exc}. Is the server running, and are its URL "
        "(or MIGRATION_URL) and credentials right?"
    )


class Command(BaseCommand):
    """Upgrade to ``head`` (or a revision); ``--downgrade`` moves down instead."""

    help = "Apply migrations to every target's databases (uses MIGRATION_URL when set)."
    operator_actions = True

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
            "--tenant",
            action="append",
            default=[],
            help="migrate this tenant's schema on TENANCY.SCHEMA_DATABASES "
            "(made if missing); repeat for several",
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
        tenant: list[str],
        **options: object,
    ) -> None:
        """Apply, revert, plan, print, or record revisions on each database.

        The databases of ``TENANCY.SCHEMA_DATABASES`` hold a schema per
        tenant, migrated with ``--tenant`` only.
        """
        if start is not None and not sql:
            raise CommandError("--from goes with --sql.")
        tenanted = () if settings.TENANCY is None else settings.TENANCY.SCHEMA_DATABASES
        if tenant and (plan or sql or fake):
            raise CommandError("--tenant migrates; it takes no --plan, --sql, --fake.")
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
        if tenant:
            for alias in tenanted if database is None else (database,):
                for name in tenant:
                    self._migrate_tenant(alias, target, name, downgrade=downgrade)
            return
        targets = (database,) if database else migrations.migration_targets()
        if len(targets) > 1 and target not in SYMBOLIC:
            raise CommandError(
                f"Revision {target!r} belongs to one target's history; name the "
                "target with --database."
            )
        for alias in (alias for name in targets for alias in router.aliases_of(name)):
            if alias in tenanted:
                self.write(
                    f"Database {alias!r} keeps a schema per tenant: use --tenant."
                )
                continue
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
                    if downgrade:
                        self._record_downgrade(alias, target, fake=True)
                    migrations.fake(target, alias, downgrade=downgrade)
                    self.write(
                        f"Database {alias!r} is recorded at {target}; nothing ran."
                    )
                else:
                    self._migrate(alias, target, downgrade=downgrade)
            except migrations.DatabaseUnreachable as exc:
                raise unreachable(alias, exc) from None
            except AlembicError as exc:
                hint = ""
                if "Multiple head" in str(exc):
                    hint = " Run 'manage.py makemigrations --merge' first."
                raise CommandError(f"{exc}{hint}") from exc

    def _migrate_tenant(
        self, alias: str, target: str, tenant: str, *, downgrade: bool
    ) -> None:
        """Move one tenant's schema of one database."""
        try:
            if downgrade:
                self._record_downgrade(alias, target, tenant=tenant)
                migrations.downgrade(target, alias, tenant=tenant)
            else:
                migrations.upgrade(target, alias, tenant=tenant)
                migrations.run_migrated_hooks(alias, tenant=tenant)
        except migrations.DatabaseUnreachable as exc:
            raise unreachable(alias, exc) from None
        except (AlembicError, ImproperlyConfigured, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.write(f"Tenant {tenant!r} on {alias!r} is at {target}.")

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
            self._record_downgrade(alias, target)
            migrations.downgrade(target, alias)
        else:
            migrations.upgrade(target, alias)
        self.write(f"Database {alias!r} is at {target}.")
        if downgrade:
            return
        if created := migrations.create_partitions(alias):
            self.write(f"Created {len(created)} partition(s) in {alias!r}.")
        migrations.run_migrated_hooks(alias)

    def _record_downgrade(
        self,
        alias: str,
        target: str,
        *,
        tenant: str | None = None,
        fake: bool = False,
    ) -> None:
        """Record that the operator downgrades ``alias``, before it happens.

        After, the trail's own tables may be gone; a record that cannot be
        written stops the downgrade.
        """
        details: dict[str, object] = {"database": alias, "revision": target}
        if tenant is not None:
            details["tenant"] = tenant
        if fake:
            details["fake"] = True
        self.record("migrate.downgrade", **details)
