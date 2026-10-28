"""The SQL commit and migration hooks a domain registers (``domainkit.apps.hooks``).

Register in ``DomainConfig.ready()``; only the domains that register are
called, typed on the SQL unit of work they join::

    from domainkit.db.hooks import on_stage

    class LedgerConfig(DomainConfig):
        def ready(self) -> None:
            on_stage(write_postings)

- ``on_stage``: add rows to a committing ``SqlAlchemyUnitOfWork``, in its
  transaction (the outbox, the audit trail). Raising rolls it back.
- ``on_committed``: follow up after it committed. A failure is logged and
  reported, and the unit of work carries on.
- ``on_stage_transaction`` and ``on_transaction_committed``: the same for a
  transaction the framework commits outside a unit of work, on a
  connection and a database alias (``messaging.enqueue``).
- ``on_migrated``: fill a database after ``migrate`` upgraded it, in one
  transaction on the schema owner's connection; check the alias first.

A ``DomainConfig`` that overrides ``stage``, ``committed``,
``stage_transaction``, ``transaction_committed``, or ``migrated`` is called
through that method as before, with a ``DomainkitDeprecationWarning``; the
methods go in 0.13.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from domainkit.apps.hooks import HookPoint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from domainkit.db.uow import SqlAlchemyUnitOfWork
    from domainkit.ddd import DeferredTask, DomainEvent

__all__ = [
    "on_committed",
    "on_migrated",
    "on_stage",
    "on_stage_transaction",
    "on_transaction_committed",
]

type StageHook = Callable[
    ["SqlAlchemyUnitOfWork", Sequence["DomainEvent"], Sequence["DeferredTask"]],
    Awaitable[None],
]
type CommittedHook = Callable[["SqlAlchemyUnitOfWork"], Awaitable[None]]
type ConnectionHook = Callable[["AsyncConnection", str], Awaitable[None]]
type DatabaseHook = Callable[[str], Awaitable[None]]

on_stage: HookPoint[StageHook] = HookPoint(
    "stage", method="stage", arguments=3, awaited=True
)
"""Add rows to a committing SQL unit of work: ``(uow, events, tasks)``."""
on_committed: HookPoint[CommittedHook] = HookPoint(
    "committed", method="committed", arguments=1, awaited=True
)
"""Follow up after a SQL unit of work committed: ``(uow)``."""
on_stage_transaction: HookPoint[ConnectionHook] = HookPoint(
    "stage_transaction", method="stage_transaction", arguments=2, awaited=True
)
"""Join a transaction committed outside a unit of work: ``(connection, alias)``."""
on_transaction_committed: HookPoint[DatabaseHook] = HookPoint(
    "transaction_committed", method="transaction_committed", arguments=1, awaited=True
)
"""Follow up after such a transaction committed: ``(alias)``."""
on_migrated: HookPoint[ConnectionHook] = HookPoint(
    "migrated", method="migrated", arguments=2, awaited=True
)
"""Fill a database ``migrate`` upgraded: ``(owner_connection, alias)``."""
