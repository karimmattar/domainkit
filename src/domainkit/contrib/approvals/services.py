"""Submit, approve, reject, cancel, expire, and run approval requests.

Each is a command run by a service, ``service(command, uow)``, as a
``ResourceRouter`` runs them. The services trust their caller for who
acts (``maker``, ``approver``): the routes take it from the principal,
and check that an approver is one the request's policy names.

The approval that completes a request queues its command through the
outbox, in the same transaction (``uow.defer``), so the command is
queued exactly when the request is approved, once. The worker runs it
(``execute_request``, under the inbox) after checking that it is what
was approved: the seals hold, the fingerprint is the arguments', the
arguments still make the command and read back the same, and enough
people approved that fingerprint. Where the guarded use case commits to
the requests' database, the request is marked executed in the use
case's own transaction, so the command commits once and the request
says so with it.
"""

import json
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationError

from domainkit import clock
from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.ddd import Command, ConcurrentUpdate, DomainError
from domainkit.money import MoneyError

from .exceptions import (
    ApprovalNotNeeded,
    ApprovalRequired,
    FingerprintMismatch,
    InvalidArguments,
    RequestNotPending,
)
from .guards import Guard, guard_for, running
from .models import (
    MAX_PERSON,
    MAX_REASON,
    ApprovalRequest,
    RequestId,
    RequestStatus,
)
from .settings import ApprovalsSettings
from .uow import ApprovalsUnitOfWork, RequestRepository, approvals_alias

EXPIRE_BATCH: Final = 500
"""Requests one round of ``expire_requests`` expires at most."""
MAKER: Final = "A maker's id has 1 to 255 characters."
APPROVER: Final = "An approver's id has 1 to 255 characters."


def config() -> ApprovalsSettings:
    """``APPROVALS``'s settings."""
    return registry.settings(ApprovalsSettings)


class SubmitRequest(Command):
    """Submit a guarded command for approval: its action, its arguments, its maker."""

    action: str
    arguments: dict[str, Any]
    maker: str


class ApproveRequest(Command):
    """Approve a request, as ``approver``, holding the ``fingerprint`` they saw."""

    request_id: UUID
    approver: str
    fingerprint: str


class RejectRequest(Command):
    """Reject a request, as ``approver``, for ``reason``."""

    request_id: UUID
    approver: str
    reason: str


class CancelRequest(Command):
    """Withdraw a request, as its ``maker``."""

    request_id: UUID
    maker: str
    reason: str = ""


def _person(value: str, message: str) -> str:
    """``value``, if it can name a person: 1 to 255 characters; else ``message``."""
    if not 1 <= len(value) <= MAX_PERSON:
        raise InvalidArguments(message)
    return value


def _reason(value: str) -> str:
    """``value``, if it is a reason of at most 500 characters."""
    if len(value) > MAX_REASON:
        raise InvalidArguments("A reason has at most 500 characters.")
    return value


async def _submit(
    guard: Guard, command: object, maker: str, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Store ``command`` as a request of ``maker``'s under the policy that applies.

    Raises:
        ApprovalNotNeeded: No policy of the guard applies to it.
        InvalidArguments: Its JSON is larger than ``MAX_ARGUMENTS_BYTES``.
    """
    policy = guard.policy_for(command)
    if policy is None:
        raise ApprovalNotNeeded("This command needs no approval: run it directly.")
    arguments = guard.dump(command)
    settings = config()
    size = len(json.dumps(arguments, separators=(",", ":")).encode("utf-8"))
    if size > settings.MAX_ARGUMENTS_BYTES:
        raise InvalidArguments("The command is larger than APPROVALS allows.")
    request = ApprovalRequest.submit(
        guard.action,
        arguments,
        maker=maker,
        policy=policy.name,
        required=policy.approvals,
        permission=policy.permission,
        group=policy.group,
        expires_at=clock.now() + (policy.expires_after or settings.EXPIRES_AFTER),
    )
    async with uow:
        await uow.requests.add(request)
        await uow.commit()
    return request


async def submit_request(
    command: SubmitRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Store a guarded command, given as JSON arguments, as a pending request.

    What is stored is the command the arguments make, read back as JSON:
    the fingerprint is of that, so what runs is what is shown.

    Raises:
        UnknownAction: No guard is registered as ``action``.
        InvalidArguments: The arguments do not make its command, or are too
            large; or the maker is malformed.
        ApprovalNotNeeded: No policy of the guard applies to the command.
    """
    maker = _person(command.maker, MAKER)
    guard = guard_for(command.action)
    try:
        made = guard.load(command.arguments)
    except ValidationError:
        raise InvalidArguments("The arguments do not make the command.") from None
    return await _submit(guard, made, maker, uow)


async def submit_command(
    action: str, command: object, *, maker: str, uow: ApprovalsUnitOfWork | None = None
) -> ApprovalRequest:
    """Store ``command`` of the guard ``action`` as a pending request of ``maker``'s.

    Raises:
        UnknownAction: No guard is registered as ``action``.
        InvalidArguments: ``command`` is not the guard's command, or too large.
        ApprovalNotNeeded: No policy of the guard applies to it.
    """
    guard = guard_for(action)
    if not isinstance(command, guard.command):
        raise InvalidArguments("The command is not the one this action takes.")
    return await _submit(
        guard, command, _person(maker, MAKER), uow or ApprovalsUnitOfWork()
    )


async def submit_directly(guard: Guard, command: object) -> ApprovalRequest:
    """Submit ``command``, which a guarded service was called with, as the actor's.

    Raises:
        ApprovalRequired: No one is acting (an anonymous caller): no maker.
    """
    request = current_request()
    actor = None if request is None else request.actor
    if actor is None:
        raise ApprovalRequired("This command needs approval: submit it for approval.")
    return await _submit(guard, command, actor, ApprovalsUnitOfWork())


async def approve_request(
    command: ApproveRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Approve a request; the approval that completes it queues its command.

    Under the request's lock, so concurrent approvals count one by one, and
    one of them completes it; that one lock is all they wait for, so they
    never deadlock. The command is queued through the outbox in the same
    transaction (``execute_request``).

    Raises:
        RequestNotFound: No request has that id.
        RequestNotPending: It is decided already, or has expired.
        MakerCannotDecide: The approver submitted it.
        AlreadyDecided: The approver decided it already.
        FingerprintMismatch: ``fingerprint`` is not the request's.
    """
    approver = _person(command.approver, APPROVER)

    async with uow:
        request = await uow.locked(command.request_id)
        if request.approve(approver, command.fingerprint):
            uow.defer(execute_request, request_id=request.id)
        await uow.commit()
    return request


async def reject_request(
    command: RejectRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Reject a request for a reason; one rejection ends it.

    Raises:
        RequestNotFound, RequestNotPending, MakerCannotDecide, AlreadyDecided:
            As ``approve_request``.
        InvalidArguments: The reason is longer than 500 characters.
    """
    approver = _person(command.approver, APPROVER)
    reason = _reason(command.reason)

    async with uow:
        request = await uow.locked(command.request_id)
        request.reject(approver, reason)
        await uow.commit()
    return request


async def cancel_request(
    command: CancelRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Withdraw a pending request, as its maker.

    Raises:
        RequestNotFound: No request has that id.
        RequestNotPending: It is decided already.
        NotTheMaker: ``maker`` did not submit it.
        InvalidArguments: The reason is longer than 500 characters.
    """
    reason = _reason(command.reason)

    async with uow:
        request = await uow.locked(command.request_id)
        request.cancel(command.maker, reason)
        await uow.commit()
    return request


async def expire_requests() -> int:
    """Expire the pending requests whose time is up; how many there were."""
    expired = 0
    while True:
        async with ApprovalsUnitOfWork() as uow:
            due = await uow.requests.due(clock.now(), EXPIRE_BATCH)
        for request_id in due:
            async with ApprovalsUnitOfWork() as uow:
                request = await uow.locked(request_id)
                if request.status is RequestStatus.PENDING:
                    request.expire()
                    await uow.commit()
                    expired += 1
        if len(due) < EXPIRE_BATCH:
            return expired


def _rebuild(guard: Guard, request: ApprovalRequest) -> object:
    """The approved command, if it is what was approved.

    Raises:
        FingerprintMismatch: A seal does not hold, the fingerprint is not the
            arguments', or fewer people than needed approved them.
        InvalidArguments: The arguments no longer make the command, or make
            one that reads back otherwise (the command changed meanwhile).
    """
    if request.approvals_verified() < request.required:
        raise FingerprintMismatch(
            "The request holds another command than the one approved."
        )
    try:
        command = guard.load(request.arguments)
    except ValidationError:
        raise InvalidArguments("The arguments do not make the command.") from None
    if guard.dump(command) != request.arguments:
        raise InvalidArguments(
            "The command reads otherwise now than when it was approved."
        )
    return command


async def _read(request_id: UUID) -> ApprovalRequest:
    """The request ``request_id``, as committed (``RequestNotFound`` if none)."""
    async with ApprovalsUnitOfWork() as uow:
        return await uow.requests.get(RequestId(request_id))


async def _finish(request_id: UUID, error: str | None = None) -> None:
    """Mark an approved request executed, or failed with ``error``; else nothing."""
    async with ApprovalsUnitOfWork() as uow:
        request = await uow.locked(request_id)
        if request.status is RequestStatus.APPROVED:
            if error is None:
                request.executed()
            else:
                request.failed(error)
            await uow.commit()


class _Marking:
    """A guarded use case's unit of work, which marks the request executed with it.

    For a unit of work on the requests' database: its first commit locks
    the request, marks it executed (``RequestExecuted``), and commits that
    with the use case's change, so neither commits without the other.
    Everything else is the unit of work's own.
    """

    def __init__(self, uow: SqlAlchemyUnitOfWork, request_id: UUID) -> None:
        """Wrap ``uow``, for the request ``request_id``."""
        self._uow = uow
        self._request_id = request_id
        self.marked = False
        """Whether a commit marked the request executed."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the unit of work's.
        """The wrapped unit of work's attribute (its repositories, ``run``, ...)."""
        return getattr(self._uow, name)

    async def __aenter__(self) -> "_Marking":
        """Enter the unit of work; its session gets the requests' repository too."""
        await self._uow.__aenter__()
        self._uow.approval_requests = RequestRepository(self._uow.session)  # type: ignore[attr-defined]
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Leave the unit of work as it leaves."""
        await self._uow.__aexit__(*exc)  # type: ignore[arg-type]

    async def commit(self) -> None:
        """Commit, marking the request executed with the first commit.

        Raises:
            RequestNotPending: The request is not approved any more (it ran).
        """
        if not self.marked:
            await self._uow.lock(f"approvals.request:{self._request_id}")
            repository: RequestRepository = self._uow.approval_requests  # type: ignore[attr-defined]
            request = await repository.get(RequestId(self._request_id))
            if request.status is not RequestStatus.APPROVED:
                raise RequestNotPending("The request is not waiting to run.")
            request.executed()
        await self._uow.commit()
        self.marked = True


def _marking(uow: object, request_id: UUID) -> "_Marking | None":
    """``uow``, to mark the request with its commit, if on the requests' database."""
    if isinstance(uow, SqlAlchemyUnitOfWork) and uow.alias == approvals_alias():
        return _Marking(uow, request_id)
    return None


async def execute_request(request_id: UUID) -> None:
    """Run an approved request's command, once (the task ``EXECUTE``).

    A request that is not approved (it ran, or failed) is left alone. A
    command that is not what was approved fails the request, and so does
    a refusal of the use case's (a ``DomainError`` other than
    ``ConcurrentUpdate``), with the refusal's code. Anything else is
    raised, for the worker to retry.
    """
    from domainkit.contrib.messaging import AlreadyHandled

    request = await _read(request_id)
    if request.status is not RequestStatus.APPROVED:
        return
    try:
        guard = guard_for(request.action)
        command = _rebuild(guard, request)
    except DomainError as exc:
        await _finish(request_id, exc.code)
        return
    uow = guard.unit_of_work()
    marking = _marking(uow, request_id)
    try:
        with running(request.id, request.action, command):
            await guard(command, marking or uow)
    except AlreadyHandled:  # It committed in another database, then the run ended.
        await _finish(request_id)
        raise
    except ConcurrentUpdate:
        raise
    except (DomainError, MoneyError) as exc:
        await _finish(request_id, exc.code)
        return
    if marking is None or not marking.marked:
        await _finish(request_id)
