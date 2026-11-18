"""The unit of work over the requests, and its repository.

Every change to a request takes the request's lock first (``lock``), so
two approvals of one request run one after the other: the second sees
the first's decision, and counts it. ADR 0121 measured the choice.

The repository reads the requests of the tenant being served alone: a
request submitted in another tenant, or in none, is not found, by id or
by query. A request of no tenant is read where no tenant is served.
Inside ``all_tenants()`` every request is (ADR 0183).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.db.routing import router
from domainkit.ddd import Criteria, F, NotFound, Selection
from domainkit.ddd.query import CriteriaLike, as_criteria
from domainkit.ddd.tenancy import TenantRequired, no_tenant, row_tenant, tenant

from .exceptions import RequestNotFound
from .models import ApprovalRequest, RequestId, RequestStatus
from .tables import requests

EVERY: Final = object()
"""What ``served_tenant`` answers inside ``all_tenants()``: every tenant's requests."""


def approvals_alias() -> str:
    """The SQL alias holding the requests: one database, never a shard group."""
    return router.require_standalone(
        router.target_for("approvals"), "DATABASE_ROUTING['approvals']"
    )


def served_tenant() -> object:
    """The tenant whose requests are read now: its id, None for none, or ``EVERY``.

    The tenant being served; ``EVERY`` inside ``all_tenants()``.
    """
    try:
        owner = row_tenant()
    except TenantRequired:
        return None
    return EVERY if owner is None else owner


@contextmanager
def serving(tenant_id: str | None) -> Iterator[None]:
    """Serve ``tenant_id`` inside the block, or no tenant for None.

    Where a request's own tenant must be served, whatever is served
    outside: its run, its expiry.
    """
    with no_tenant() if tenant_id is None else tenant(tenant_id):
        yield


class RequestRepository(SqlAlchemyRepository[ApprovalRequest, RequestId]):
    """Approval requests, with their decisions: the tenant's being served alone."""

    model = ApprovalRequest

    def not_found(self, id_: RequestId) -> NotFound:
        """The error for a missing request."""
        return RequestNotFound("No approval request has that id.")

    def _scope(
        self, criteria: CriteriaLike | None, *, include_deleted: bool
    ) -> Criteria | None:
        """``criteria``, held to the requests of the tenant being served.

        Every query reads through here: a list, a count, a lock by id.
        """
        served = served_tenant()
        if served is not EVERY:
            own = F("tenant_id") == served
            criteria = own if criteria is None else as_criteria(criteria) & own
        return super()._scope(criteria, include_deleted=include_deleted)

    async def _get(self, id_: RequestId) -> ApprovalRequest | None:
        """The request ``id_``, if it is of the tenant being served.

        Another tenant's is missing, as a ``TenantOwned`` aggregate is,
        before any precondition is checked: its existence is not told.
        """
        found = await super()._get(id_)
        served = served_tenant()
        if found is None or served is EVERY or found.tenant_id == served:
            return found
        return None

    async def due(self, now: datetime, limit: int) -> list[RequestId]:
        """Ids of up to ``limit`` pending requests whose time was up at ``now``.

        Every tenant's, in no order: each round expires what it found, and
        the next finds the rest.
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

    async def made_by(
        self, maker: str, action: str, digest: str
    ) -> list[ApprovalRequest]:
        """The pending and approved requests of ``maker`` holding that command.

        Those of ``action`` whose fingerprint is ``digest``, oldest first.
        """
        waiting = (F("status") == RequestStatus.PENDING) | (
            F("status") == RequestStatus.APPROVED
        )
        return await self.filter(
            Selection(
                (F("maker") == maker)
                & (F("action") == action)
                & (F("fingerprint") == digest)
                & waiting,
                ordering=["submitted_at"],
            )
        )


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
