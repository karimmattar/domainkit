"""``loaddata``: write a fixture's rows into a database."""

import json
from argparse import ArgumentParser
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Table
from sqlalchemy.exc import IntegrityError

from domainkit.conf import DEFAULT_DB_ALIAS, settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import fixtures
from domainkit.db.errors import integrity_violation
from domainkit.db.orm import metadata
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Insert the rows, parents first, in one transaction.

    A row whose primary key is stored already replaces the stored row; the
    command says how many will be replaced before it writes. Staging and
    production refuse without ``--force``.
    """

    help = "Load JSON fixtures; rows replace stored rows with the same primary key."
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept fixture files, an alias, and the safety switches."""
        parser.add_argument("paths", nargs="+", type=Path, metavar="FIXTURE")
        parser.add_argument(
            "--database", default=DEFAULT_DB_ALIAS, help="the alias to write"
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="only say what would be written"
        )
        parser.add_argument(
            "--force", action="store_true", help="required in staging and production"
        )

    async def handle(
        self,
        *,
        paths: list[Path],
        database: str,
        dry_run: bool,
        force: bool,
        **options: object,
    ) -> None:
        """Read every fixture, say what loading does, then load it."""
        for path in paths:
            try:
                tables = fixtures.read_fixture(_read(path), database)
            except fixtures.FixtureError as exc:
                raise CommandError(f"{path}: {exc}") from None
            loads = await fixtures.plan_load(database, tables)
            for entry in loads:
                replaced = f", replacing {entry.replaced}" if entry.replaced else ""
                self.write(f"{path}: {entry.table}: {_rows(entry.rows)}{replaced}")
            if dry_run:
                continue
            if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS and not force:
                raise CommandError(
                    f"Loading replaces stored rows in {settings.ENVIRONMENT}; "
                    "repeat with --force after taking a backup."
                )
            async with self.recorded(
                "loaddata",
                fixture=path.name,
                database=database,
                rows=sum(entry.rows for entry in loads),
                replaced=sum(entry.replaced for entry in loads),
            ):
                await self._load(path, database, tables)
            self.write(f"Loaded {_rows(sum(e.rows for e in loads))} from {path}.")

    async def _load(
        self,
        path: Path,
        database: str,
        tables: list[tuple[Table, list[dict[str, Any]]]],
    ) -> None:
        """Write the rows; a constraint the database refuses names the fixture."""
        try:
            await fixtures.load(database, tables)
        except IntegrityError as exc:
            violation = integrity_violation(exc, metadata)
            what = violation.kind.replace("_", " ") if violation else "a constraint"
            raise CommandError(
                f"{path}: the database refused the rows ({what}); nothing was loaded."
            ) from None


def _rows(count: int) -> str:
    """``1 row``, ``2 rows``."""
    return f"{count} row{'' if count == 1 else 's'}"


def _read(path: Path) -> object:
    """The JSON document in ``path``; its numbers with a fraction as ``Decimal``.

    Read as floats, money would lose digits before its column, which
    refuses floats, ever saw it (``fixtures.read_fixture`` turns them back
    into floats for columns that hold floats).
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except OSError as exc:
        raise CommandError(f"Cannot read {path}: {exc.strerror or exc}.") from None
    except (ValueError, RecursionError):
        raise CommandError(f"{path} is not a JSON document.") from None
