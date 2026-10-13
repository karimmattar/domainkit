"""Building and writing audit rows.

Who acted comes from the request context: the authenticated actor,
``anonymous`` for unauthenticated requests, ``system`` outside requests.
Never put secrets in event fields or ``details``: they are kept for good.
"""

import fnmatch
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import connections
from domainkit.ddd import DomainEvent
from domainkit.utils.text import snake_case

from .settings import AuditSettings
from .tables import audit_log


class Outcome(StrEnum):
    """Result of an audited action."""

    SUCCESS = "success"
    FAILURE = "failure"
    """The action was attempted and failed (a dependency was down, ...)."""
    DENIED = "denied"
    """A policy refused the action."""


def _actor() -> str:
    """Return who is acting in the current context."""
    request = current_request()
    if request is None:
        return "system"
    return request.actor or "anonymous"


def action_for(event_type: type[DomainEvent]) -> str:
    """Name an event's action ``<domain label>.<event in snake case>``."""
    module = event_type.__module__
    label = next(
        (
            config.label
            for config in registry
            if module == config.name or module.startswith(f"{config.name}.")
        ),
        module.rpartition(".")[0] or module,
    )
    return f"{label}.{snake_case(event_type.__name__)}"


def audit_row(
    action: str,
    outcome: Outcome,
    *,
    target: str | None = None,
    details: Mapping[str, object] | None = None,
    event_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one audit row in the current request context."""
    request = current_request()
    return {
        "id": uuid.uuid4(),
        "occurred_at": occurred_at or datetime.now(UTC),
        "actor": _actor(),
        "action": action,
        "outcome": outcome.value,
        "target": target,
        "request_id": request.request_id if request else None,
        "client_ip": request.client_ip if request else None,
        "event_id": event_id,
        "details": to_jsonable_python(dict(details or {})),
    }


def event_rows(
    events: Sequence[DomainEvent], config: AuditSettings
) -> list[dict[str, Any]]:
    """Audit rows for committed events, minus excluded actions."""
    rows = []
    for event in events:
        action = action_for(type(event))
        if any(fnmatch.fnmatchcase(action, pattern) for pattern in config.EXCLUDE):
            continue
        details = to_jsonable_python(event)
        details.pop("event_id", None)
        details.pop("occurred_at", None)
        rows.append(
            audit_row(
                action,
                Outcome.SUCCESS,
                details=details,
                event_id=event.event_id,
                occurred_at=event.occurred_at,
            )
        )
    return rows


async def write(
    executor: AsyncSession | AsyncConnection, rows: Sequence[dict[str, Any]]
) -> None:
    """Insert ``rows`` through a session or connection."""
    if rows:
        await executor.execute(insert(audit_log).values(list(rows)))


async def record(
    action: str,
    outcome: Outcome = Outcome.SUCCESS,
    *,
    target: str | None = None,
    **details: object,
) -> None:
    """Record an action in its own transaction (denials, failures, reads).

    Committed domain events are recorded automatically; use this for what
    no event describes, such as a refused login.
    """
    config = registry.settings(AuditSettings)
    row = audit_row(action, outcome, target=target, details=details)
    async with connections.engine(config.DATABASE).begin() as connection:
        await write(connection, [row])
