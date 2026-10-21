"""The database rungs' data: a PostgreSQL schema of their own, migrated and seeded.

``python -m benchmarks`` runs these for each source tree it measures, so a
tree is migrated by its own code::

    DOMAINKIT_BENCH_POSTGRES_URL=... python -m benchmarks.database create SCHEMA FIXTURE
    DOMAINKIT_BENCH_POSTGRES_URL=... python -m benchmarks.database drop SCHEMA

``create`` replaces the schema, writes the project's migrations (to
``DOMAINKIT_BENCH_MIGRATIONS``) and applies them, then adds a user in a
group that may view and add items, an opaque token for them, and an item.
``FIXTURE`` gets the token and the item's id, as JSON. Only schemas named
``domainkit_bench_...`` are ever created or dropped.
"""

import asyncio
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from benchmarks.ladder import POSTGRES_VARIABLE

SCHEMA: Final = re.compile(r"domainkit_bench_[a-z0-9_]{1,40}")
PERMISSIONS: Final = ("catalog.view_item", "catalog.add_item")


def database(url: str, schema: str) -> dict[str, Any]:
    """The ``DATABASES`` entry that works in ``schema`` on ``url``."""
    return {"URL": url, "CONNECT_ARGS": {"server_settings": {"search_path": schema}}}


async def replace_schema(url: str, schema: str, *, create: bool) -> None:
    """Drop ``schema`` with what it holds, then create it empty if ``create``."""
    if SCHEMA.fullmatch(schema) is None:
        raise ValueError(f"Refusing schema {schema!r}: not domainkit_bench_...")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            if create:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()


async def seed() -> dict[str, str]:
    """Add the user, their group, token, and an item; return the token and id."""
    from benchmarks.project.catalog.models import Item, ItemId
    from benchmarks.project.catalog.uow import CatalogUnitOfWork
    from domainkit.contrib.auth import AuthUnitOfWork
    from domainkit.db import connections

    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user("bench", "unused-bench-password")
        clerks = await uow.groups.create("clerks")
        clerks.grant(*[await uow.permissions.get(key) for key in PERMISSIONS])
        user.join(clerks)
        token = await uow.tokens.issue(user.id, ttl=None, name="bench")
        await uow.commit()
    item = Item(id=ItemId(uuid4()), name="widget", price=42)
    async with CatalogUnitOfWork() as catalog:
        await catalog.items.add(item)
        await catalog.commit()
    await connections.close_all()
    return {"token": token, "item": str(item.id)}


def create(url: str, schema: str, fixture: Path) -> None:
    """Make ``schema`` on ``url`` ready for the database rungs."""
    asyncio.run(replace_schema(url, schema, create=True))
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
    match args:
        case ["create", schema, fixture] if url:
            create(url, schema, Path(fixture))
        case ["drop", schema] if url:
            asyncio.run(replace_schema(url, schema, create=False))
        case _:
            sys.stderr.write(__doc__ or "")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
