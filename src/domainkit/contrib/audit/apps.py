"""Install ``domainkit.contrib.audit`` to keep an audit trail."""

from collections.abc import Sequence
from typing import cast

from domainkit.apps import DomainConfig, registry
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork

from .recorder import event_rows, write
from .settings import AuditSettings


class AuditConfig(DomainConfig):
    """Record committed domain events in the same transaction."""

    label = "audit"
    verbose_name = "Audit trail"
    settings_class = AuditSettings

    async def stage(
        self,
        uow: UnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Add one audit row per committed event (unless excluded)."""
        config = registry.settings(AuditSettings)
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        if not config.EVENTS or sql_uow.database != config.DATABASE:
            return
        await write(sql_uow.session, event_rows(events, config))
