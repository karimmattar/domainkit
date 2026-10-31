"""``dumpdata``: write a database's rows as a fixture (JSON, parents first)."""

import asyncio
import hashlib
import os
from argparse import ArgumentParser
from pathlib import Path

from sqlalchemy import Table

from domainkit.conf import DEFAULT_DB_ALIAS, settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import fixtures
from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.operators import require_named


class Command(BaseCommand):
    """Print the fixture, or write it to a file.

    State is left out: the audit trail, tokens, the outbox, idempotency
    keys, and tables marked ``info={"fixtures": False}``. So are guarded
    tables (users and their password hashes, permissions, grants, dynamic
    settings), unless ``--include-guarded``: named, one is refused; with no
    names, they are left out and the command says so. A dump with guarded
    tables is audited (``operator.dumpdata``), and needs a named operator
    in staging and production; so does one, there, of a table whose model
    has fields marked ``Personal`` (SEC-12a-7). Encrypted columns are
    written sealed, as stored; ``--decrypt`` writes their values, and is
    audited and needs a named operator there too. A fixture can hold
    personal data; keep it as safe as the database. ``--output`` writes a new file
    only its owner can read, and refuses a path that exists, a symbolic
    link too.
    """

    help = "Write rows as a JSON fixture: every table, or the tables or domains named."
    operator_actions = True

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept tables or domain labels, an alias, and an output file."""
        parser.add_argument(
            "names",
            nargs="*",
            metavar="TABLE_OR_DOMAIN",
            help="tables or domain labels (default: every table of the database)",
        )
        parser.add_argument(
            "--database", default=DEFAULT_DB_ALIAS, help="the alias to read"
        )
        parser.add_argument(
            "-o",
            "--output",
            type=Path,
            help="a new file to write it to (mode 0600); default: standard output",
        )
        parser.add_argument(
            "--indent", type=int, default=2, help="spaces per level (default: 2)"
        )
        parser.add_argument(
            "--decrypt",
            action="store_true",
            help="write encrypted columns' values, not their ciphertext",
        )
        parser.add_argument(
            "--include-guarded",
            action="store_true",
            help="dump guarded tables too: users and their password hashes, "
            "permissions, grants, dynamic settings, ...",
        )

    async def handle(
        self,
        *,
        names: list[str],
        database: str,
        output: Path | None,
        indent: int,
        include_guarded: bool,
        decrypt: bool = False,
        **options: object,
    ) -> None:
        """Read the tables and write their rows."""
        try:
            tables = self._tables(database, names, include_guarded=include_guarded)
            guarded = include_guarded and any(map(fixtures.guarded, tables))
            people = (
                sorted(table.name for table in tables if fixtures.personal(table))
                if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS
                else []
            )
            decrypted = (
                sorted(table.name for table in tables if fixtures.encrypted(table))
                if decrypt
                else []
            )
            if not (guarded or people or decrypted):
                await self._write(database, tables, output, indent)
                return
            what = (
                "--include-guarded"
                if guarded
                else "--decrypt"
                if decrypted
                else "of personal data"
            )
            require_named(self.operator, f"dumpdata {what}")
            async with self.recorded(
                "dumpdata",
                database=database,
                output="stdout" if output is None else output.name,
                include_guarded=include_guarded,
                personal_tables=people,
                **({"decrypted_tables": decrypted} if decrypted else {}),
            ) as details:
                details.update(
                    await self._write(
                        database, tables, output, indent, decrypt=bool(decrypted)
                    )
                )
        except (fixtures.FixtureError, ValueError) as exc:
            raise CommandError(str(exc)) from None

    def _tables(
        self, database: str, names: list[str], *, include_guarded: bool
    ) -> list[Table]:
        """The tables to dump; guarded ones only if included.

        Raises:
            CommandError: A guarded table is named, alone or by its domain,
                without ``--include-guarded``.
        """
        tables = fixtures.select_tables(database, names)
        if include_guarded:
            return tables
        held = [table.name for table in tables if fixtures.guarded(table)]
        if held and names:
            raise CommandError(
                f"Guarded tables ({', '.join(held)}) hold logins, password "
                "hashes, permissions, and secrets. Nothing was written; repeat "
                "with --include-guarded if that is what you mean."
            )
        if held:
            self.stderr.write(
                f"Left out guarded tables ({', '.join(held)}); "
                "--include-guarded dumps them.\n"
            )
        return [table for table in tables if not fixtures.guarded(table)]

    async def _write(
        self,
        database: str,
        tables: list[Table],
        output: Path | None,
        indent: int,
        *,
        decrypt: bool = False,
    ) -> dict[str, object]:
        """Dump ``tables`` to ``output``; what the record of it holds.

        Table names, row counts, and the SHA-256 of the text written: no row.
        """
        document = await fixtures.dump(database, tables, decrypt=decrypt)
        text = fixtures.to_json(document, indent=indent or None)
        rows = {entry["table"]: len(entry["rows"]) for entry in document["tables"]}
        if output is None:
            self.write(text)
        else:
            try:
                await asyncio.to_thread(_write_new, output, f"{text}\n")
            except FileExistsError:
                raise CommandError(f"{output} exists; name a new file.") from None
            self.write(
                f"Wrote {sum(rows.values())} row(s) of {len(tables)} table(s) "
                f"to {output}."
            )
        return {
            "rows": sum(rows.values()),
            "tables": rows,
            "sha256": hashlib.sha256(f"{text}\n".encode()).hexdigest(),
        }


def _write_new(path: Path, text: str) -> None:
    """Write ``text`` to a new file at ``path`` that only its owner can read.

    Nothing may be there, not even a symbolic link, so a link planted in a
    shared directory cannot send the fixture to another file, and the
    umask cannot make it readable by others.

    Raises:
        FileExistsError: Something is at ``path`` already.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text)
