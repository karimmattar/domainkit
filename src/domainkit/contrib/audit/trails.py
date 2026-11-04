"""The trails of a database: its own schema's, and each tenant's.

On a database of ``TENANCY.SCHEMA_DATABASES`` every table lives in each
tenant's schema too, the audit trail's included: a unit of work run as a
tenant writes its rows there. Each such trail is a chain of its own, sealed,
anchored (under ``<alias>/<schema>``), verified, and forgotten like the
database's own (A6-4). ``in_trail`` points the trail's code at one schema;
``connect`` gives it connections there.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.db import connections
from domainkit.db.uow import tenant_schemas

from .tables import audit_log

_schema: Final[ContextVar[str | None]] = ContextVar("audit_schema", default=None)
_FOUND: Final = text("SELECT to_regclass(:name)")


@contextmanager
def in_trail(schema: str | None) -> Iterator[None]:
    """Work on the trail in ``schema`` (None: the connection's own)."""
    token = _schema.set(schema)
    try:
        yield
    finally:
        _schema.reset(token)


def label(database: str) -> str:
    """The name of the current trail of ``database``: ``<alias>[/<schema>]``.

    It names the trail in reports, checkpoints, and its ledger keys.
    """
    schema = _schema.get()
    return database if schema is None else f"{database}/{schema}"


@asynccontextmanager
async def connect(
    database: str, *, begin: bool = False
) -> AsyncIterator[AsyncConnection]:
    """A connection to ``database`` whose tables are the current trail's.

    In a tenant's schema, every table it names is that schema's
    (``schema_translate_map``); ``begin`` also opens a transaction.
    """
    engine = connections.engine(database)
    async with engine.begin() if begin else engine.connect() as connection:
        schema = _schema.get()
        if schema is not None:
            await connection.execution_options(schema_translate_map={None: schema})
        yield connection


async def schemas(database: str) -> list[str | None]:
    """The schemas with a trail of ``database``: its own (None), each tenant's."""
    return await tenant_schemas(database)


async def trails(databases: tuple[str, ...]) -> list[tuple[str, str | None]]:
    """Each trail of ``databases``: the alias, and the schema (None: its own).

    On a database that keeps a schema per tenant, only the schemas that
    hold the trail's table: its own may hold none, and a tenant's none yet.
    """
    found: list[tuple[str, str | None]] = []
    for database in databases:
        places = await schemas(database)
        if places == [None]:
            found.append((database, None))
            continue
        async with connections.engine(database).connect() as connection:
            for schema in places:
                where = "" if schema is None else f'"{schema}".'
                name = where + audit_log.name
                if await connection.scalar(_FOUND, {"name": name}) is not None:
                    found.append((database, schema))
    return found
