"""Create the content type rows that ``migrate`` leaves missing."""

from sqlalchemy import Connection, Table, insert, inspect, select
from sqlalchemy.ext.asyncio import AsyncConnection

from .registry import installed_models
from .tables import content_types


async def sync_content_types(connection: AsyncConnection) -> dict[tuple[str, str], int]:
    """Insert a row for every installed model; return ids by ``(domain, model)``.

    Rows of models that are gone are kept: permissions and grants hang off
    them, and a domain that comes back finds them again.
    """
    existing = await _content_type_ids(connection)
    missing = [
        {"domain": domain, "model": model}
        for domain, model in (installed.key for installed in installed_models())
        if (domain, model) not in existing
    ]
    if not missing:
        return existing
    await connection.execute(insert(content_types), missing)
    return await _content_type_ids(connection)


async def has_tables(connection: AsyncConnection, *tables: Table) -> bool:
    """Whether every table exists (``makemigrations`` may not have run yet)."""

    def check(sync_connection: Connection) -> bool:
        inspector = inspect(sync_connection)
        return all(
            inspector.has_table(table.name, schema=table.schema) for table in tables
        )

    return await connection.run_sync(check)


async def _content_type_ids(connection: AsyncConnection) -> dict[tuple[str, str], int]:
    """Every stored content type's id, by ``(domain, model)``."""
    rows = await connection.execute(
        select(content_types.c.domain, content_types.c.model, content_types.c.id)
    )
    return {(row.domain, row.model): row.id for row in rows}
