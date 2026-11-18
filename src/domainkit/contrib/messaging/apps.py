"""Install ``domainkit.contrib.messaging`` to get the outbox and the worker."""

import sys
from collections.abc import Mapping, Sequence
from importlib import import_module
from importlib.util import find_spec
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import lifecycle
from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import (
    on_deployment_warnings,
    on_erase_subject,
    on_export_subject,
)
from domainkit.checks import CheckMessage
from domainkit.conf import DEPLOYED_ENVIRONMENTS, settings
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.db.hooks import on_committed, on_stage
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.lifecycle import Phase

from . import runs
from .outbox import notify_committed, outbox_targets, rows_for, write
from .settings import MessagingSettings

_committing: WeakKeyDictionary[AsyncSession, list[dict[str, Any]]] = WeakKeyDictionary()
"""Messages each unit of work's session wrote, to publish once it commits.

By session: a commit that failed leaves its rows with its session, which
the next ``async with`` of the unit of work replaces (``UnitOfWork.run``)."""


async def close_publishers() -> None:
    """Flush and stop the background publishers, if anything started one.

    Looked up, not imported: a web process that hands no message to a
    broker never loads the dispatcher, nor the worker it imports (PERF-15).
    """
    dispatch = sys.modules.get(f"{__package__}.dispatch")
    if dispatch is not None:
        await dispatch.close_publishers()


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
        publisher once it lands (``on_committed``). ``check`` warns about
        tasks written as ``def`` (``blocking.W002``). The framework's own
        tasks elsewhere, such as the one that removes the stored files a
        commit dropped (``domainkit.storage.cleanup``), are registered when
        first deferred or run (``registry.FRAMEWORK_TASKS``).
        """
        config = self.get_settings()
        router.require_standalone(config.DATABASE, "MESSAGING.DATABASE")
        if config.BROKER is not None:
            # Imported here: without a broker, none is loaded (PERF-15).
            from domainkit.broker import brokers

            brokers.config(config.BROKER)
        lifecycle.register("publishers", close_publishers, phase=Phase.DRAIN)
        on_stage(self.stage_outbox)
        on_committed(self.publish_committed)
        if find_spec("prometheus_client") is not None:
            import_module(f"{self.name}.metrics")
        on_erase_subject(self.erase_copies)
        on_export_subject(self.export_copies)
        on_deployment_warnings(self.blocking_tasks)
        on_deployment_warnings(self.unnamed_tasks)
        on_deployment_warnings(self.unnamed_handlers)
        on_deployment_warnings(self.small_pools)
        on_deployment_warnings(self.unkept_priorities)

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

    def blocking_tasks(self) -> list[CheckMessage]:
        """Warn about tasks written as ``def``, in every environment (``check``).

        The worker awaits each run on its event loop: a ``def`` holds every
        message of the process while it works, an ordered key's included,
        then fails. Durable handlers are on the event bus, which ``check``
        looks at itself.
        """
        from domainkit.http.blocking import awaited_sync  # Only ``check`` needs it.

        from .registry import all_tasks

        found = awaited_sync(
            (f"the task {spec.key!r}", spec.function) for spec in all_tasks()
        )
        return [CheckMessage("blocking.W002", message) for message in found]

    def unnamed_tasks(self) -> list[CheckMessage]:
        """``messaging.W001``: tasks named after their module's path, where deployed.

        A task's name is the key its messages carry. Without ``name=`` it is
        ``module.function``: moving or renaming the function renames it,
        and the messages queued under the old name find no task (CONC-15).
        """
        from .registry import all_tasks

        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        return [
            CheckMessage(
                "messaging.W001",
                f"The task {spec.key!r} is named after its module's path: moving "
                "or renaming it renames its messages, which then wait for a task "
                "of the old name and are dead-lettered after "
                "MESSAGING.UNKNOWN_TARGET_GRACE.",
                hint="Give it a name of its own: @task(name=...).",
            )
            for spec in all_tasks()
            if not spec.named
        ]

    def small_pools(self) -> list[CheckMessage]:
        """``messaging.W003``: pools smaller than a worker's messages, where deployed.

        A worker runs up to ``CONCURRENCY`` messages at once, each holding a
        connection while its handler's work and its outcome are written,
        besides the one it listens and claims with. A pool of fewer makes
        them wait for one up to ``POOL_TIMEOUT``, then fail and be retried
        (user report W9).
        """
        from domainkit.conf.global_settings import is_network_database
        from domainkit.db.connections import pool_capacity

        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        concurrency = registry.settings(MessagingSettings).CONCURRENCY
        found = []
        for alias, database in settings.DATABASES.items():
            capacity = pool_capacity(database)
            url = database.URL.get_secret_value()
            if not is_network_database(url) or concurrency + 1 <= capacity:
                continue
            found.append(
                CheckMessage(
                    "messaging.W003",
                    f"MESSAGING.CONCURRENCY ({concurrency}) runs more messages at "
                    f"once than the pool of DATABASES[{alias!r}] serves: it holds "
                    f"{capacity} connections per worker (POOL_SIZE + "
                    "MAX_OVERFLOW), and each running message holds one, besides "
                    "the one the worker listens and claims with. The others wait "
                    "for one up to POOL_TIMEOUT, then fail and are retried.",
                    hint=f"Lower CONCURRENCY to {max(capacity - 1, 1)}, or raise the "
                    "pool for the workers (their settings may differ from the web "
                    "processes').",
                )
            )
        return found

    def unkept_priorities(self) -> list[CheckMessage]:
        """``messaging.W004``: task priorities the broker drops, where deployed.

        The outbox hands out its due messages by priority, but a broker
        hands out what it holds. RabbitMQ's quorum queues do so by the
        priority each message carries; Redis Streams, NATS JetStream, Kafka,
        SQS, and a ``BACKEND`` whose ``priorities`` is false hand them out
        in the order they came (user report W9, ADR 0157).
        """
        from domainkit.broker import brokers
        from domainkit.broker.handler import broker_class

        from .registry import all_tasks

        alias = self.get_settings().BROKER
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS or alias is None:
            return []
        try:
            kind = broker_class(brokers.config(alias))
        except ImproperlyConfigured:  # The worker refuses it when it starts.
            return []
        named = sorted(spec.key for spec in all_tasks() if spec.priority)
        if kind.priorities or not named:
            return []
        return [
            CheckMessage(
                "messaging.W004",
                f"Tasks name a priority ({', '.join(map(repr, named))}), and "
                f"MESSAGING.BROKER ({alias!r}, a {kind.__name__}) hands out "
                "messages in the order they came: a priority orders only the "
                "messages that wait in the outbox, not those the broker holds.",
                hint="Give urgent work a queue of its own, run by workers of its "
                "own (@task(queue=...), runworker --queue ...); or use RabbitMQ "
                "4.0 or later, whose queues take priorities.",
            )
        ]

    def unnamed_handlers(self) -> list[CheckMessage]:
        """``messaging.W002``: durable handlers without ``name=``, where deployed.

        A durable handler's key is the one its messages carry. Without
        ``name=`` it is ``module.QualName``: moving or renaming the function,
        or making it in-process, renames it, and the messages queued under
        the old key find no handler (user report W5).
        """
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        unnamed = {
            key.key for key in event_bus.handler_keys() if key.current and not key.named
        }
        return [
            CheckMessage(
                "messaging.W002",
                f"The durable handler {key!r} is named after its module's path: "
                "moving or renaming it, or making it in-process, renames its "
                "messages, which then wait for a handler of the old name and are "
                "dead-lettered after MESSAGING.UNKNOWN_TARGET_GRACE.",
                hint=f"Give it a name of its own: @handles(..., durable=True, "
                f"name=...), with formerly=[{key!r}] for the messages queued "
                "under this one.",
            )
            for key in sorted(unnamed)
        ]

    async def erase_copies(self, subject: str, reason: str) -> Mapping[str, int]:
        """Blank dead messages about ``subject``; count pending ones (``erasure``)."""
        from .erasure import erase_subject

        return await erase_subject(subject)

    async def export_copies(self, subject: str) -> Mapping[str, object]:
        """The queued and dead messages about ``subject``, with their arguments."""
        from .erasure import export_subject

        found = await export_subject(subject)
        return {"messages": found} if found else {}

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: the outbox commits with each domain's data.

        And ``MESSAGING.DATABASE``, the home outbox, when no domain lives there.
        """
        return frozenset(outbox_targets())

    def get_settings(self) -> MessagingSettings:
        """Return the validated ``MESSAGING`` settings."""
        return registry.settings(MessagingSettings)
