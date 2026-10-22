"""The database rungs' data: a PostgreSQL schema of their own, migrated and seeded.

``python -m benchmarks`` runs these for each source tree it measures, so a
tree is migrated by its own code::

    DOMAINKIT_BENCH_POSTGRES_URL=... python -m benchmarks.database create SCHEMA FIXTURE
    DOMAINKIT_BENCH_POSTGRES_URL=... python -m benchmarks.database drop SCHEMA

``create`` makes the schema, which must not exist yet, and writes
``FIXTURE`` empty at once, so the caller knows the schema is its own to
drop. It writes the project's migrations (to ``DOMAINKIT_BENCH_MIGRATIONS``)
and applies them, then adds a user (without a usable password) in a group
that may view and add items, an opaque token for them that expires within
``TOKEN_TTL``, and an item. ``FIXTURE`` then gets the token and the item's
id, as JSON. Only schemas named ``domainkit_bench_...`` are ever created or
dropped, and ``python -m benchmarks`` names each run's with a random suffix
and drops only that one. A failure is printed with the URL's password
masked.
"""

import asyncio
import io
import json
import os
import re
import sys
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from benchmarks.ladder import POSTGRES_VARIABLE, masked

SCHEMA: Final = re.compile(r"domainkit_bench_[a-z0-9_]{1,40}")
PERMISSIONS: Final = ("catalog.view_item", "catalog.add_item")
TOKEN_TTL: Final = timedelta(hours=1)
"""How long the bench token lasts: a run takes minutes, and one killed
leaves no token that works for long."""


def database(url: str, schema: str) -> dict[str, Any]:
    """The ``DATABASES`` entry that works in ``schema`` on ``url``."""
    return {"URL": url, "CONNECT_ARGS": {"server_settings": {"search_path": schema}}}


async def make_schema(url: str, schema: str) -> None:
    """Create ``schema``, empty; refused if it exists, whoever made it."""
    await _run(url, f'CREATE SCHEMA "{_checked(schema)}"')


async def drop_schema(url: str, schema: str) -> None:
    """Drop ``schema`` with what it holds, if it exists."""
    await _run(url, f'DROP SCHEMA IF EXISTS "{_checked(schema)}" CASCADE')


def _checked(schema: str) -> str:
    """``schema``, if it is named as the ladder names its schemas.

    Raises:
        ValueError: It is not ``domainkit_bench_...``.
    """
    if SCHEMA.fullmatch(schema) is None:
        raise ValueError(f"Refusing schema {schema!r}: not domainkit_bench_...")
    return schema


async def _run(url: str, statement: str) -> None:
    """Run one statement in a transaction of its own on ``url``."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def seed() -> dict[str, str]:
    """Add the user, their group, token, and an item; return the token and id."""
    from benchmarks.project.catalog.models import Item, ItemId
    from benchmarks.project.catalog.uow import CatalogUnitOfWork
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.db import connections

    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user("bench")  # No password to log in with.
        clerks = await uow.groups.create("clerks")
        clerks.grant(*[await uow.permissions.get(key) for key in PERMISSIONS])
        user.join(clerks)
        token = await uow.tokens.issue(user.id, ttl=TOKEN_TTL, name="bench")
        await uow.commit()
    item = Item(id=ItemId(uuid4()), name="widget", price=42)
    async with CatalogUnitOfWork() as catalog:
        await catalog.items.add(item)
        await catalog.commit()
    await connections.close_all()
    return {"token": token, "item": str(item.id)}


def create(url: str, schema: str, fixture: Path) -> None:
    """Make ``schema`` on ``url`` ready for the database rungs.

    ``fixture`` is written as soon as the schema is made: from then on,
    the schema is the caller's to drop, made or half made.
    """
    asyncio.run(make_schema(url, schema))
    fixture.write_text("{}")
    from domainkit import setup
    from domainkit.conf import configure, override_settings
    from domainkit.management import call_command

    configure("benchmarks.project.settings")
    with override_settings(DATABASES={"default": database(url, schema)}):
        setup()
        output = io.StringIO()
        call_command("makemigrations", "-m", "bench", stdout=output)
        call_command("migrate", stdout=output)
        fixture.write_text(json.dumps(asyncio.run(seed())))


def main(argv: list[str] | None = None) -> int:
    """``create SCHEMA FIXTURE`` or ``drop SCHEMA``, on the database named.

    The URL comes from ``DOMAINKIT_BENCH_POSTGRES_URL``, not the command line.
    """
    args = sys.argv[1:] if argv is None else argv
    url = os.environ.get(POSTGRES_VARIABLE, "")
    try:
        match args:
            case ["create", schema, fixture] if url:
                create(url, schema, Path(fixture))
            case ["drop", schema] if url:
                asyncio.run(drop_schema(url, schema))
            case _:
                sys.stderr.write(__doc__ or "")
                return 2
    except Exception:  # noqa: BLE001 - any failure, printed without the password.
        sys.stderr.write(masked(traceback.format_exc(), url))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
