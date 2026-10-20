"""``dumpdata``: write a database's rows as a fixture (JSON, parents first)."""

import asyncio
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
            "-o", "--output", type=Path, help="write the fixture here, not to stdout"
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
        await asyncio.to_thread(output.write_text, f"{text}\n", encoding="utf-8")
        rows = sum(len(entry["rows"]) for entry in document["tables"])
        self.write(f"Wrote {rows} row(s) of {len(tables)} table(s) to {output}.")
