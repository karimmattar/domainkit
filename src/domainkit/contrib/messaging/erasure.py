"""Find, export, and erase what the outbox keeps about a person.

A message is about the subjects its ``Personal`` markers name: those of its
event's class, for a durable handler call, and those of its task's
parameters, for a task (``Annotated[str, Personal(of="customer_id")]``).
Done and cancelled messages keep no arguments already (``payload`` is
``{}``). A dead one about the subject is blanked, as a ``sensitive``
task's is when it dies, and ``messages --retry`` then refuses it. A pending
one is left to run, and is blanked once done; it is counted, so the
operator can erase again once the worker has run it.
"""

from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import Row, not_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import registry
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.ddd.personal import subjects_in

from .admin import blanked
from .registry import get_task
from .settings import MessagingSettings
from .tables import BLANKED, DEAD, PENDING, messages
from .worker import _event_classes

KEPT: Final = (PENDING, DEAD)
"""The statuses whose messages keep their arguments."""


def outboxes() -> tuple[str, ...]:
    """Every alias holding an outbox: ``MESSAGING.DATABASE`` and each host's."""
    aliases = [registry.settings(MessagingSettings).DATABASE]
    for target in router.hosting_targets():
        aliases.extend(router.aliases_of(target))
    return tuple(dict.fromkeys(aliases))


def subjects_of(row: Row[Any], events: Mapping[str, type[Any]]) -> frozenset[str]:
    """Whom a message's arguments are about, by the markers of what it runs."""
    if row.kind == "event":
        event_type = events.get(row.headers.get("event_type", ""))
        if event_type is None:
            return frozenset()
        return subjects_in(event_type, row.payload)
    spec = get_task(row.target)
    if spec is None:
        return frozenset()
    return subjects_in(spec.function, row.payload)


async def _about(connection: AsyncConnection, subject: str) -> list[Row[Any]]:
    """The messages still holding arguments about ``subject``, oldest first."""
    events = _event_classes()
    found = await connection.stream(
        select(messages)
        .where(messages.c.status.in_(KEPT), not_(blanked()))
        .order_by(messages.c.created_at, messages.c.id)
    )
    return [row async for row in found if subject in subjects_of(row, events)]


async def erase_subject(subject: str) -> dict[str, int]:
    """Blank dead messages about ``subject``; count the pending ones left to run."""
    erased = pending = 0
    for alias in outboxes():
        async with connections.engine(alias).begin() as connection:
            for row in await _about(connection, subject):
                if row.status == PENDING:
                    pending += 1
                    continue
                done = await connection.execute(
                    update(messages)
                    .where(messages.c.id == row.id, messages.c.status == DEAD)
                    .values(payload={}, headers={**row.headers, BLANKED: True})
                )
                erased += done.rowcount
    return {"messages": erased, "pending": pending}


async def export_subject(subject: str) -> list[dict[str, Any]]:
    """The messages holding arguments about ``subject``, with those arguments."""
    found = []
    for alias in outboxes():
        async with connections.engine(alias).connect() as connection:
            rows = await _about(connection, subject)
        found += [
            {
                "id": str(row.id),
                "database": alias,
                "kind": row.kind,
                "target": row.target,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "arguments": row.payload,
            }
            for row in rows
        ]
    return found
