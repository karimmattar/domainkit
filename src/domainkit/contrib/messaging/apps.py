"""Install ``domainkit.contrib.messaging`` to get the outbox and the worker."""

from collections.abc import Mapping, Sequence
from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import lifecycle
from domainkit.apps import DomainConfig, registry
from domainkit.broker import brokers
from domainkit.checks import CheckMessage
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.db.hooks import on_committed, on_stage
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, event_bus
from domainkit.lifecycle import Phase

if TYPE_CHECKING:
    from fastapi import FastAPI

from . import runs
from .dispatch import close_publishers
from .outbox import notify_committed, rows_for, write
from .settings import MessagingSettings

_committing: WeakKeyDictionary[AsyncSession, list[dict[str, Any]]] = WeakKeyDictionary()
"""Messages each unit of work's session wrote, to publish once it commits.

By session: a commit that failed leaves its rows with its session, which
the next ``async with`` of the unit of work replaces (``UnitOfWork.run``)."""


class MessagingConfig(DomainConfig):
    """Write durable handler calls and deferred tasks with each commit."""

    label = "messaging"
    verbose_name = "Messaging"
    settings_class = MessagingSettings
    requires = ()

    def broker_queues(self) -> tuple[str, Sequence[str]]:
        """``MESSAGING.BROKER``, and every queue the worker's tasks use (``broker``).

        Raises:
            CommandError: ``MESSAGING.BROKER`` names no broker.
        """
        from domainkit.management.base import CommandError

        from . import registry as tasks

        alias = self.get_settings().BROKER
        if alias is None:
            raise CommandError("declare needs MESSAGING.BROKER.")
        return alias, sorted(tasks.queues())

    def ready(self) -> None:
        """Check the home outbox and broker; expose gauges with Prometheus.

        Shutdown (``domainkit.lifecycle``) closes the background publishers
        in its drain phase, in the application and in management commands
        alike, within the time that phase always has (``drain_time``). Each
        commit joins the run of the message a worker is processing, if any,
        writes its outbox rows (``on_stage``), and hands them to the
        publisher once it lands (``on_committed``).
        """
        config = self.get_settings()
        router.require_standalone(config.DATABASE, "MESSAGING.DATABASE")
        if config.BROKER is not None:
            brokers.config(config.BROKER)
        lifecycle.register("publishers", close_publishers, phase=Phase.DRAIN)
        on_stage(self.stage_outbox)
        on_committed(self.publish_committed)
        if find_spec("prometheus_client") is not None:
            import_module(f"{self.name}.metrics")

    async def stage_outbox(
        self,
        uow: SqlAlchemyUnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Join the message being run, if any; add outbox rows to the transaction.

        Inside a worker's run of a message, the commit renews the run's
        lease or fails (``runs.LeaseLost``), and writes its inbox row
        (``runs.AlreadyHandled`` if it is there already).
        """
        await runs.stage(uow)
        rows = rows_for(events, tasks, uow.events, self.get_settings())
        if not rows:
            return
        session = uow.session
        dialect = session.get_bind().dialect.name
        await write(session, dialect, rows)
        _committing[session] = rows

    async def publish_committed(self, uow: SqlAlchemyUnitOfWork) -> None:
        """Hand the messages the unit of work wrote to the background publisher."""
        runs.committed(uow)
        rows = _committing.pop(uow.session, [])
        await notify_committed(self.get_settings(), uow.alias, rows)

    async def erase_subject(self, subject: str, reason: str) -> Mapping[str, int]:
        """Blank dead messages about ``subject``; count pending ones (``erasure``)."""
        from .erasure import erase_subject

        return await erase_subject(subject)

    async def export_subject(self, subject: str) -> Mapping[str, object]:
        """The queued and dead messages about ``subject``, with their arguments."""
        from .erasure import export_subject

        found = await export_subject(subject)
        return {"messages": found} if found else {}

    def check_application(self, app: "FastAPI") -> list[str]:
        """``inbox`` handlers and tasks whose retries could outlast the inbox.

        A message may run again until its last retry: each attempt waits
        its backoff's longest delay, holds its lease, and, with a broker,
        may wait ``REPUBLISH_AFTER`` for its publication. A run after
        ``INBOX_RETENTION_SECONDS`` would find the inbox row deleted and
        commit again.
        """
        from . import registry as tasks

        config = self.get_settings()
        retention = config.INBOX_RETENTION_SECONDS
        targets = [
            (key, config.MAX_ATTEMPTS, config.RETRY_MAX_SECONDS)
            for key, (_, function) in tasks.durable_handlers(event_bus).items()
            if runs.uses_inbox(function)
        ] + [
            (
                spec.key,
                spec.max_attempts or config.MAX_ATTEMPTS,
                config.RETRY_MAX_SECONDS if spec.backoff is None else spec.backoff.cap,
            )
            for spec in tasks.all_tasks()
            if runs.uses_inbox(spec.function)
        ]
        problems: list[str] = []
        for key, attempts, longest in targets:
            window = runs.redelivery_window(config, attempts, longest)
            if window >= retention:
                problems.append(
                    CheckMessage(
                        "messaging.E001",
                        f"{key} uses the inbox, but its message may run again up "
                        f"to {window:g}s after a run that committed, and "
                        f"MESSAGING.INBOX_RETENTION_SECONDS is {retention:g}: a "
                        "late run would find its inbox row deleted and commit "
                        "again. Raise INBOX_RETENTION_SECONDS above it, or give "
                        "it fewer attempts or shorter retry delays.",
                    )
                )
        return problems

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: the outbox commits with each domain's data."""
        return frozenset(router.hosting_targets())

    def get_settings(self) -> MessagingSettings:
        """Return the validated ``MESSAGING`` settings."""
        return registry.settings(MessagingSettings)
