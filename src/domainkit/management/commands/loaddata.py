"""``loaddata``: write a fixture's rows into a database."""

import hashlib
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
from domainkit.management.operators import require_named


class Command(BaseCommand):
    """Insert the rows, parents first, in one transaction.

    The command says what each table gets before it writes. A row whose
    primary key is stored refuses the load, unless ``--replace``; a fixture
    that writes a guarded table (users, permissions, grants, ...) is
    refused unless ``--include-guarded``. Staging and production refuse
    without ``--force``, and without an operator named by ``--operator`` or
    ``DOMAINKIT_OPERATOR``. Each load is audited with its tables, counts,
    and the fixture's SHA-256.
    """

    help = (
        "Load JSON fixtures: new rows only, unless --replace; guarded tables "
        "only with --include-guarded."
    )
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
        parser.add_argument(
            "--replace",
            action="store_true",
            help="replace stored rows with the same primary key (refused without)",
        )
        parser.add_argument(
            "--include-guarded",
            action="store_true",
            help="write guarded tables: users, permissions, grants, tokens, ...",
        )

    async def handle(
        self,
        *,
        paths: list[Path],
        database: str,
        dry_run: bool,
        force: bool,
        replace: bool,
        include_guarded: bool,
        **options: object,
    ) -> None:
        """Read every fixture, say what loading does, then load it."""
        for path in paths:
            document, digest = _read(path)
            try:
                tables = fixtures.read_fixture(document, database)
            except fixtures.FixtureError as exc:
                raise CommandError(f"{path}: {exc}") from None
            loads = await fixtures.plan_load(database, tables)
            for entry in loads:
                self.write(f"{path}: {entry.table}: {_planned(entry, replace=replace)}")
            if dry_run:
                continue
            if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS and not force:
                raise CommandError(
                    f"Loading writes rows in {settings.ENVIRONMENT}; repeat with "
                    "--force after taking a backup."
                )
            require_named(self.operator, "loaddata")
            _refuse(path, loads, replace=replace, include_guarded=include_guarded)
            async with self.recorded(
                "loaddata",
                fixture=path.name,
                sha256=digest,
                database=database,
                rows=sum(entry.rows for entry in loads),
                replaced=sum(entry.replaced for entry in loads),
                tables={
                    entry.table: {"rows": entry.rows, "replaced": entry.replaced}
                    for entry in loads
                },
                replace=replace,
                include_guarded=include_guarded,
            ):
                await self._load(path, database, tables, replace=replace)
            self.write(f"Loaded {_rows(sum(e.rows for e in loads))} from {path}.")

    async def _load(
        self,
        path: Path,
        database: str,
        tables: list[tuple[Table, list[dict[str, Any]]]],
        *,
        replace: bool,
    ) -> None:
        """Write the rows; a constraint the database refuses names the fixture."""
        try:
            await fixtures.load(database, tables, replace=replace)
        except IntegrityError as exc:
            violation = integrity_violation(exc, metadata)
            what = violation.kind.replace("_", " ") if violation else "a constraint"
            raise CommandError(
                f"{path}: the database refused the rows ({what}); nothing was loaded."
            ) from None


def _planned(entry: fixtures.TableLoad, *, replace: bool) -> str:
    """What loading does to one table: ``2 rows, replacing 1 (guarded)``."""
    stored = ""
    if entry.replaced:
        stored = (
            f", replacing {entry.replaced}"
            if replace
            else f", {entry.replaced} already stored"
        )
    return f"{_rows(entry.rows)}{stored}{' (guarded)' if entry.guarded else ''}"


def _refuse(
    path: Path,
    loads: list[fixtures.TableLoad],
    *,
    replace: bool,
    include_guarded: bool,
) -> None:
    """Refuse a load of guarded tables, or over stored rows, not asked for."""
    if not include_guarded and (
        names := [entry.table for entry in loads if entry.guarded]
    ):
        raise CommandError(
            f"{path} writes guarded tables ({', '.join(names)}): their rows are "
            "logins, permissions, and secrets. Nothing was loaded; repeat with "
            "--include-guarded if that is what you mean."
        )
    if not replace and (stored := sum(entry.replaced for entry in loads)):
        raise CommandError(
            f"{path}: {_rows(stored)} would replace stored rows with the same "
            "primary key. Nothing was loaded; repeat with --replace to replace them."
        )


def _rows(count: int) -> str:
    """``1 row``, ``2 rows``."""
    return f"{count} row{'' if count == 1 else 's'}"


def _read(path: Path) -> tuple[object, str]:
    """The JSON document in ``path``, and the SHA-256 of the file's bytes.

    Numbers with a fraction are ``Decimal``: read as floats, money would
    lose digits before its column, which refuses floats, ever saw it
    (``fixtures.read_fixture`` turns them back into floats for columns that
    hold floats). The file is read once, so the digest is of what loads.
    """
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise CommandError(f"Cannot read {path}: {exc.strerror or exc}.") from None
    try:
        document = json.loads(content.decode("utf-8"), parse_float=Decimal)
    except (ValueError, RecursionError):
        raise CommandError(f"{path} is not a JSON document.") from None
    return document, hashlib.sha256(content).hexdigest()
