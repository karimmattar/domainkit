"""Building and writing audit rows.

Who acted comes from the request context: the authenticated actor,
``anonymous`` for unauthenticated requests, ``system`` outside requests.
Never put secrets in event fields or ``details``: they are kept for good.

Personal values are written as pseudonyms (see ``personal``): the client's
address, as the actor's (or the day's anonymous callers'), and event fields
marked ``Personal``, as the person's they name.
"""

import fnmatch
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
from domainkit.ddd.personal import personal_fields
from domainkit.utils.text import snake_case

from .personal import anonymous_subject, pseudonym, subject_keys
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


@dataclass(frozen=True, slots=True)
class PersonalValue:
    """A value of a row to write as a pseudonym of its subject."""

    name: str
    """The column (``client_ip``) or, ``in_details``, the key in ``details``."""
    subject: str
    value: object
    in_details: bool = True


@dataclass(frozen=True, slots=True)
class Draft:
    """An audit row whose personal values are not pseudonyms yet."""

    row: dict[str, Any]
    personal: tuple[PersonalValue, ...] = field(default=())

    def written(self, keys: Mapping[str, bytes]) -> dict[str, Any]:
        """The row to insert, each personal value a pseudonym under ``keys``."""
        row = {**self.row, "details": dict(self.row["details"])}
        for item in self.personal:
            place = row["details"] if item.in_details else row
            place[item.name] = pseudonym(keys[item.subject], item.name, item.value)
        return row


def audit_row(
    action: str,
    outcome: Outcome,
    *,
    target: str | None = None,
    details: Mapping[str, object] | None = None,
    personal: Mapping[str, tuple[str | None, object]] | None = None,
    event_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> Draft:
    """Build one audit row in the current request context.

    ``personal`` maps keys of ``details`` to their subject and value. A
    subject of None, and the client's address, belong to the actor; without
    one, to the day's anonymous callers.
    """
    request = current_request()
    occurred_at = occurred_at or datetime.now(UTC)
    caller = (request.actor if request else None) or anonymous_subject(
        occurred_at.astimezone(UTC).date()
    )
    row = {
        "id": uuid.uuid4(),
        "occurred_at": occurred_at,
        "actor": _actor(),
        "action": action,
        "outcome": outcome.value,
        "target": target,
        "request_id": request.request_id if request else None,
        "client_ip": None,
        "event_id": event_id,
        "details": to_jsonable_python(dict(details or {})),
    }
    personal = dict(personal or {})
    if taken := sorted(personal.keys() & row["details"].keys()):
        raise ValueError(f"Audit details cannot be both plain and personal: {taken}.")
    values = []
    for name, (subject, value) in personal.items():
        if value is None:
            row["details"][name] = None  # Nothing personal to hide.
        else:
            values.append(PersonalValue(name, subject or caller, value))
    if request and request.client_ip is not None:
        values.append(
            PersonalValue("client_ip", caller, request.client_ip, in_details=False)
        )
    return Draft(row, tuple(values))


def event_rows(events: Sequence[DomainEvent], config: AuditSettings) -> list[Draft]:
    """Audit rows for committed events, minus excluded actions.

    A field marked ``Personal`` is the person's that its ``of`` field names.
    """
    rows = []
    for event in events:
        action = action_for(type(event))
        if any(fnmatch.fnmatchcase(action, pattern) for pattern in config.EXCLUDE):
            continue
        details = to_jsonable_python(event)
        details.pop("event_id", None)
        details.pop("occurred_at", None)
        personal = {
            name: (_subject(details[marker.of]), details.pop(name))
            for name, marker in personal_fields(type(event)).items()
        }
        rows.append(
            audit_row(
                action,
                Outcome.SUCCESS,
                details=details,
                personal=personal,
                event_id=event.event_id,
                occurred_at=event.occurred_at,
            )
        )
    return rows


async def write(
    executor: AsyncSession | AsyncConnection, drafts: Sequence[Draft]
) -> None:
    """Insert ``drafts`` through a session or connection, in its transaction.

    The subjects' keys are read, or made, in the same transaction.
    """
    if not drafts:
        return
    subjects = {item.subject for draft in drafts for item in draft.personal}
    keys = await subject_keys(executor, subjects)
    await executor.execute(
        insert(audit_log).values([draft.written(keys) for draft in drafts])
    )


def _subject(value: object) -> str | None:
    """The subject a person's id names, as text; None for no id."""
    return None if value is None else str(value)


async def record(
    action: str,
    outcome: Outcome = Outcome.SUCCESS,
    *,
    target: str | None = None,
    personal: Mapping[str, object] | None = None,
    subject: str | None = None,
    **details: object,
) -> None:
    """Record an action in its own transaction (denials, failures, reads).

    Committed domain events are recorded automatically; use this for what
    no event describes, such as a refused login. ``personal`` details are
    written as pseudonyms of ``subject``: by default the actor, or the
    day's anonymous callers.
    """
    config = registry.settings(AuditSettings)
    row = audit_row(
        action,
        outcome,
        target=target,
        details=details,
        personal={name: (subject, value) for name, value in (personal or {}).items()},
    )
    async with connections.engine(config.DATABASE).begin() as connection:
        await write(connection, [row])
