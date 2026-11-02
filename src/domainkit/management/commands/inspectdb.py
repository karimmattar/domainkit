"""``inspectdb``: write aggregates for the tables of an existing database.

``manage.py inspectdb [TABLE ...] [--database ALIAS]`` reads the tables
and prints a ``models.py`` to start from: one aggregate per table, each
column a field with its hint (``MaxLength`` for a sized text,
``Digits`` for a decimal, ``| None`` for a nullable one), and the
``table_for`` call that maps it back. Nothing is written to the database.

It is a start, not a model: name the classes, move fields into value
objects and child entities, and add the methods that keep their rules.
A table without a single ``id`` primary key is listed with what it needs.
"""

import asyncio
import re
from argparse import ArgumentParser
from typing import Any, Final

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    Interval,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    Time,
    Uuid,
)

from domainkit.conf import DEFAULT_DB_ALIAS
from domainkit.db import connections
from domainkit.management.base import BaseCommand, CommandError

SKIPPED: Final = frozenset({"id", "version"})
"""Columns ``AggregateRoot`` declares itself."""

_HINTS: Final[list[tuple[type, str, str | None]]] = [
    (Boolean, "bool", None),
    (SmallInteger, "int", None),
    (BigInteger, "int", None),
    (Integer, "int", None),
    (Float, "float", None),
    (DateTime, "datetime", "from datetime import datetime"),
    (Date, "date", "from datetime import date"),
    (Time, "time", "from datetime import time"),
    (Interval, "timedelta", "from datetime import timedelta"),
    (Uuid, "UUID", "from uuid import UUID"),
    (JSON, "dict[str, Any]", "from typing import Any"),
    (LargeBinary, "bytes", None),
    (Text, "str", None),
]
"""Column types and their hints, the narrowest first (``Float`` is a ``Numeric``)."""


class Command(BaseCommand):
    """Print aggregates and ``table_for`` calls for a database's tables."""

    help = "Write aggregates for the tables of an existing database, to start from."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept table names and a database alias."""
        parser.add_argument(
            "tables", nargs="*", metavar="TABLE", help="only these (default: all)"
        )
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="the alias to read (default: default)",
        )

    def handle(self, *, tables: list[str], database: str, **options: object) -> None:
        """Read the tables, then print a module for them.

        Raises:
            CommandError: A table named is not in the database.
        """
        found = asyncio.run(_reflect(database, tables))
        missing = sorted(set(tables) - set(found.tables))
        if missing:
            raise CommandError(f"No table named {missing} in {database!r}.")
        self.write(render(sorted(found.tables.values(), key=lambda table: table.name)))


async def _reflect(alias: str, only: list[str]) -> MetaData:
    """The tables of ``alias`` (only ``only``, if given)."""
    metadata = MetaData()
    try:
        async with connections.engine(alias).connect() as connection:
            await connection.run_sync(
                lambda sync: metadata.reflect(
                    sync, only=lambda name, _: not only or name in only
                )
            )
    finally:
        await connections.close_all()
    return metadata


def render(tables: list[Table]) -> str:
    """A module of one aggregate per table, imports first."""
    imports = {
        "from domainkit.db import table_for",
        "from domainkit.ddd import AggregateRoot",
    }
    blocks = [_aggregate(table, imports) for table in tables]
    merged: dict[str, set[str]] = {}
    for line in imports:
        module, _, names = line.removeprefix("from ").partition(" import ")
        merged.setdefault(module, set()).add(names)
    standard = [m for m in sorted(merged) if not m.startswith("domainkit")]
    ours = [m for m in sorted(merged) if m.startswith("domainkit")]
    header = '"""Aggregates read from the database by inspectdb: check each one."""'
    lines = [header, ""]
    for group in (standard, ours):
        lines += [f"from {m} import {', '.join(sorted(merged[m]))}" for m in group]
        lines.append("")
    return "\n".join([*lines, "", "\n\n\n".join(blocks)]) + "\n"


def _aggregate(table: Table, imports: set[str]) -> str:
    """One aggregate, its fields, and its ``table_for`` call."""
    name = _class_name(table.name)
    key = [column.name for column in table.primary_key.columns]
    identity = table.columns.get("id")
    lines = []
    if key == ["id"] and identity is not None:
        lines.append(f"class {name}(AggregateRoot[{_hint(identity, imports)}]):")
        lines += [f'    """The table {table.name}."""', ""]
    else:
        lines.append(f"class {name}(AggregateRoot[int]):")
        lines += [f'    """The table {table.name}."""', ""]
        lines.append(
            f"    # inspectdb: the primary key is {key or 'missing'}; an "
            "aggregate needs a single 'id' column."
        )
    columns = [column for column in table.columns if column.name not in SKIPPED]
    ordered = [c for c in columns if not c.nullable] + [
        c for c in columns if c.nullable
    ]
    lines += [_field(column, imports) for column in ordered] or ["    pass"]
    variable = re.sub(r"\W", "_", table.name)
    if not variable.isidentifier():
        variable = f"t_{variable}"
    mapping = f'{variable} = table_for({name}, "{table.name}")'
    return "\n".join(lines) + f"\n\n\n{mapping}"


def _field(column: Column[Any], imports: set[str]) -> str:
    """``name: hint`` for a column, ``| None = None`` if it may be missing."""
    hint = _hint(column, imports)
    attribute = (
        column.name if column.name.isidentifier() else re.sub(r"\W", "_", column.name)
    )
    if column.nullable:
        return f"    {attribute}: {hint} | None = None"
    return f"    {attribute}: {hint}"


def _hint(column: Column[Any], imports: set[str]) -> str:
    """The Python hint for a column's type, with what it needs imported."""
    kind = column.type
    for sql, hint, needs in _HINTS:
        if isinstance(kind, sql):
            if needs:
                imports.add(needs)
            return hint
    if isinstance(kind, Numeric):
        imports.add("from decimal import Decimal")
        if kind.precision is not None:
            imports |= {
                "from typing import Annotated",
                "from domainkit.ddd import Digits",
            }
            return f"Annotated[Decimal, Digits({kind.precision}, {kind.scale or 0})]"
        return "Decimal"
    if isinstance(kind, String):
        if kind.length:
            imports |= {
                "from typing import Annotated",
                "from domainkit.ddd import MaxLength",
            }
            return f"Annotated[str, MaxLength({kind.length})]"
        return "str"
    imports.add("from typing import Any")
    return f"Any  # inspectdb: {kind!r} has no known hint"


def _class_name(table: str) -> str:
    """``shop_order_lines`` as ``ShopOrderLine``: words capitalised, one plural cut."""
    words = [word for word in re.split(r"\W|_", table) if word]
    if words and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    name = "".join(word[:1].upper() + word[1:] for word in words) or "Table"
    return name if name[0].isalpha() else f"T{name}"
