"""``dumpdata``: write a database's rows as a fixture (JSON, parents first)."""

import asyncio
import os
from argparse import ArgumentParser
from pathlib import Path

from domainkit.conf import DEFAULT_DB_ALIAS
from domainkit.db import fixtures
from domainkit.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Print the fixture, or write it to a file.

    State is left out: the audit trail, tokens, the outbox, idempotency
    keys, and tables marked ``info={"fixtures": False}``. A fixture can hold
    personal data and password hashes; keep it as safe as the database.
    ``--output`` writes a new file only its owner can read, and refuses a
    path that exists, a symbolic link too.
    """

    help = "Write rows as a JSON fixture: every table, or the tables or domains named."

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

    async def handle(
        self,
        *,
        names: list[str],
        database: str,
        output: Path | None,
        indent: int,
        **options: object,
    ) -> None:
        """Read the tables and write their rows."""
        try:
            tables = fixtures.select_tables(database, names)
            document = await fixtures.dump(database, tables)
            text = fixtures.to_json(document, indent=indent or None)
        except (fixtures.FixtureError, ValueError) as exc:
            raise CommandError(str(exc)) from None
        if output is None:
            self.write(text)
            return
        try:
            await asyncio.to_thread(_write_new, output, f"{text}\n")
        except FileExistsError:
            raise CommandError(f"{output} exists; name a new file.") from None
        rows = sum(len(entry["rows"]) for entry in document["tables"])
        self.write(f"Wrote {rows} row(s) of {len(tables)} table(s) to {output}.")


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
