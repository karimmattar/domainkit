"""The unit of work over the requests, and its repository.

Every change to a request takes the request's lock first (``lock``), so
two approvals of one request run one after the other: the second sees
the first's decision, and counts it. ADR 0121 measured the choice.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.db.routing import router
from domainkit.ddd import NotFound

from .exceptions import RequestNotFound
from .models import ApprovalRequest, RequestId, RequestStatus
from .tables import requests


def approvals_alias() -> str:
    """The SQL alias holding the requests: one database, never a shard group."""
    return router.require_standalone(
        router.target_for("approvals"), "DATABASE_ROUTING['approvals']"
    )


class RequestRepository(SqlAlchemyRepository[ApprovalRequest, RequestId]):
    """Approval requests, with their decisions."""

    model = ApprovalRequest

    def not_found(self, id_: RequestId) -> NotFound:
        """The error for a missing request."""
        return RequestNotFound("No approval request has that id.")

    async def due(self, now: datetime, limit: int) -> list[RequestId]:
        """Ids of up to ``limit`` pending requests whose time was up at ``now``.

        In no order: each round expires what it found, and the next finds the rest.
        """
        found = await self.session.scalars(
            select(requests.c.id)
            .where(
                requests.c.status == RequestStatus.PENDING,
                requests.c.expires_at <= now,
            )
            .limit(limit)
        )
        return [RequestId(each) for each in found]


class ApprovalsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the requests, wherever ``approvals`` is routed."""

    requests: RequestRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository to the session."""
        self.requests = RequestRepository(session)

    async def locked(self, request_id: UUID) -> ApprovalRequest:
        """The request ``request_id``, under its lock until the transaction ends.

        Raises:
            RequestNotFound: No request has that id.
        """
        await self.lock(f"approvals.request:{request_id}")
        return await self.requests.get(RequestId(request_id))
