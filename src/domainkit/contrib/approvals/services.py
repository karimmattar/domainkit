"""Submit, approve, reject, cancel, expire, and run approval requests.

Each is a command run by a service, ``service(command, uow)``, as a
``ResourceRouter`` runs them. The services trust their caller for who
acts (``maker``, ``approver``): the routes take it from the principal,
and check that an approver is one the request's policy names.

With its setting on (``SUBMIT_ACTION``, ``APPROVE_ACTION``,
``REJECT_ACTION``, ``CANCEL_ACTION``), ``submit_request``,
``approve_request``, ``reject_request``, and ``cancel_request`` spend the
request's authorization of the act, and refuse a call that has none, from
code too. ``record_submission``, ``record_approval``, ``record_rejection``,
and ``record_cancellation`` do their work asking for none: the services
run them, and the project's own code calls them (ADR 0163, amended
2026-11-17).

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

A guard ``by_maker`` queues nothing: its maker's own call runs the
approved command (``run_by_maker``), spending the request first.

A request belongs to the tenant it was submitted in. The services find
the requests of the tenant being served alone (the repository's), and
the approved command runs in the request's own tenant, whichever tenant
the message that runs it names (ADR 0183).
"""

import functools
import json
import logging
import operator
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationError

from domainkit import clock
from domainkit.apps import registry
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.ddd import Command, ConcurrentUpdate, Criteria, DomainError, F, Selection
from domainkit.ddd.tenancy import all_tenants, current_tenant, tenant_alone
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import BasePermission, OfflineContext
from domainkit.http.errors import transient
from domainkit.log import ThrottledLog
from domainkit.money import MoneyError
from domainkit.reporting import report_error

from .exceptions import (
    ApprovalNotNeeded,
    ApprovalPending,
    ApprovalRequired,
    FingerprintMismatch,
    InvalidArguments,
    MakerNotAllowed,
    RequestNotFound,
    RequestNotPending,
    RequestTampered,
)
from .guards import Guard, Policy, Running, guard_for, policy_of, running, when_spent
from .models import (
    MAX_PERSON,
    MAX_REASON,
    ApprovalRequest,
    RequestId,
    RequestStatus,
    fingerprint,
)
from .rules import rule_instance
from .settings import ApprovalsSettings
from .uow import ApprovalsUnitOfWork, RequestRepository, approvals_alias, servable

logger = logging.getLogger(__name__)
AUTH: Final = "domainkit.contrib.auth"
EXECUTE: Final = "domainkit.approvals.execute"
"""The task that runs an approved request's command (``tasks``)."""
EXPIRE_BATCH: Final = 500
"""Requests one round of ``expire_requests`` expires at most."""
STAFF_FLAGS: Final = ("is_superuser", "is_staff")
"""The flags of the users an approver rule is asked about (``approvers_of``)."""
MAKER: Final = "A maker's id has 1 to 255 characters."
APPROVER: Final = "An approver's id has 1 to 255 characters."
rule_log: Final = ThrottledLog(logger)
"""Maker and approver rules that raised, a line per rule a while."""


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


def _prepared(guard: Guard, command: object, maker: str) -> tuple[Policy, Any]:
    """The policy that applies to ``command``, and its arguments, if it may wait.

    Raises:
        ApprovalNotNeeded: No policy of the guard applies to it.
        InvalidArguments: Its JSON is larger than ``MAX_ARGUMENTS_BYTES``,
            or a field of the guard's ``maker_fields`` names someone else.
    """
    if not guard.names_its_maker(command, maker):
        raise InvalidArguments(
            "The command names someone other than its maker as the one who acts."
        )
    policy = guard.policy_for(command)
    if policy is None:
        raise ApprovalNotNeeded("This command needs no approval: run it directly.")
    arguments = guard.dump(command)
    size = len(json.dumps(arguments, separators=(",", ":")).encode("utf-8"))
    if size > config().MAX_ARGUMENTS_BYTES:
        raise InvalidArguments("The command is larger than APPROVALS allows.")
    return policy, arguments


async def _submit(
    guard: Guard, command: object, maker: str, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Store ``command`` as a request of ``maker``'s under the policy that applies.

    The request is of the tenant being served, or of none.

    Raises:
        ApprovalNotNeeded: As for ``_prepared``.
        InvalidArguments: As for ``_prepared``.
        TooMuchToShow: With ``APPROVE_ACTION`` and auth's
            ``AUTHORIZATIONS.WHOLE`` on, approving it could not be shown
            whole (auth's, 422).
    """
    policy, arguments = _prepared(guard, command, maker)
    settings = config()
    if settings.APPROVE_ACTION:  # Never a request no approver's screen shows whole.
        from .authorizing import check_approvable

        await check_approvable(guard, command)
    request = ApprovalRequest.submit(
        guard.action,
        arguments,
        maker=maker,
        policy=policy.name,
        required=policy.approvals,
        permission=policy.permission,
        group=policy.group,
        expires_at=clock.now() + (policy.expires_after or settings.EXPIRES_AFTER),
        tenant_id=current_tenant(),
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
    the fingerprint is of that, so what runs is what is shown. With
    ``APPROVALS.SUBMIT_ACTION`` on, it spends the maker's authorization of
    this submission (``approvals.submit``), with the request's commit
    where the requests share auth's database: called without one, it is
    refused.

    Raises:
        UnknownAction: No guard is registered as ``action``.
        InvalidArguments: The arguments do not make its command, or are too
            large; or the maker is malformed.
        ApprovalNotNeeded: No policy of the guard applies to the command.
        TooMuchToShow: With ``APPROVE_ACTION`` and auth's
            ``AUTHORIZATIONS.WHOLE`` on, approving it could not be shown
            whole on one screen (auth's, 422).
        AuthorizationRequired: With ``SUBMIT_ACTION``, no authorization of
            this submission came with the request (auth's, 401).
        ActionNotAuthorized: With ``SUBMIT_ACTION``, it is of another
            submission (auth's, 403).
    """
    if config().SUBMIT_ACTION:
        from .authorizing import submit_authorized

        return await submit_authorized(command, uow)
    return await record_submission(command, uow)


async def record_submission(
    command: SubmitRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Submit a command, asking for no authorization: for the project's own code.

    What ``submit_request`` does, whatever ``APPROVALS.SUBMIT_ACTION``
    says: ``submit_request`` runs it, once the maker's authorization is
    spent where the setting is on. Call it from code the project trusts (a
    task, a management command, an event handler), never from a route a
    user reaches: that route would submit without the phone. It trusts its
    caller for who acts, as every service here: the guard's permission and
    its ``maker_rule``, which ``POST /requests`` asks of the maker, are
    asked only when the command runs (``maker_not_allowed``), and only of
    a maker that is a user's id, with ``domainkit.contrib.auth``
    installed, on a call without credentials: there, rules about scopes
    do not limit, and a step-up refuses. A maker that names no user is
    never asked. A guard ``http=False`` is submitted too.

    Raises:
        UnknownAction: As for ``submit_request``.
        InvalidArguments: As for ``submit_request``.
        ApprovalNotNeeded: As for ``submit_request``.
        TooMuchToShow: As for ``submit_request``.
    """
    guard, made, maker = _made(command)
    return await _submit(guard, made, maker, uow)


def check_submission(command: SubmitRequest) -> Any:  # noqa: ANN401 - JSON.
    """The arguments ``command`` would be stored with, if it may be submitted.

    What submitting checks, storing nothing: asking for an authorization of
    a submission that would be refused is refused as it would be.

    Raises:
        UnknownAction: As for ``submit_request``.
        InvalidArguments: As for ``submit_request``.
        ApprovalNotNeeded: As for ``submit_request``.
    """
    guard, made, maker = _made(command)
    return _prepared(guard, made, maker)[1]


def _made(command: SubmitRequest) -> tuple[Guard, object, str]:
    """The guard, the command its arguments make, and the maker.

    Raises:
        UnknownAction: No guard is registered as ``action``.
        InvalidArguments: The arguments do not make its command; or the
            maker is malformed.
    """
    maker = _person(command.maker, MAKER)
    guard = guard_for(command.action)
    try:  # Who acts is the maker, whatever the arguments say.
        made = guard.load(guard.with_maker(command.arguments, maker))
    except ValidationError:
        raise InvalidArguments("The arguments do not make the command.") from None
    return guard, made, maker


async def submit_command(
    action: str, command: object, *, maker: str, uow: ApprovalsUnitOfWork | None = None
) -> ApprovalRequest:
    """Store ``command`` of the guard ``action`` as a pending request of ``maker``'s.

    Raises:
        UnknownAction: No guard is registered as ``action``.
        InvalidArguments: ``command`` is not the guard's command, or too large.
        ApprovalNotNeeded: No policy of the guard applies to it.
        TooMuchToShow: As for ``submit_request``.
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
    return await _submit(guard, command, _actor(), ApprovalsUnitOfWork())


def _actor() -> str:
    """Who is acting: the maker of what a direct call submits.

    Raises:
        ApprovalRequired: No one is (an anonymous caller).
    """
    request = current_request()
    actor = None if request is None else request.actor
    if actor is None:
        raise ApprovalRequired("This command needs approval: submit it for approval.")
    return actor


async def run_by_maker(guard: Guard, command: object) -> Running:
    """Spend the caller's approved request holding ``command`` (a guard ``by_maker``).

    The request is the oldest approved one of the caller's, of the guard's
    action, whose fingerprint is that of ``command``, that has not expired.
    It is checked as the worker checks a run (its seals, its arguments, its
    approvals, its maker), and marked executed before the call goes on: a
    call refused after that needs a new approval. A request that fails a
    check fails, and the next is tried.

    Waiting, the act's authorization the caller confirmed for this call,
    if auth's action authorizations took one, is kept for the call made
    again once approved, until the request expires, within
    ``AUTH.AUTHORIZATIONS.HELD_TTL`` (ADR 0174). Kept for one request, it
    serves that one alone (since 2.8, ADR 0179): an approved or pending
    request it is not kept for is passed over, and a call that would wait
    on, or submit, another is refused, and asks for a new authorization.
    An approved request whose time is up is passed over too: it never
    runs.

    Raises:
        ApprovalRequired: No one is acting.
        MakerNotAllowed: The caller may no longer make the command.
        ActionNotAuthorized: The act's authorization is kept for another
            request (auth's, 403).
        ApprovalPending: None is approved: the caller's request holding
            ``command`` that waits, or else one submitted now.
    """
    maker = _actor()
    arguments = guard.dump(command)
    digest = fingerprint(guard.action, arguments)
    async with ApprovalsUnitOfWork() as uow:
        found = await uow.requests.made_by(maker, guard.action, digest)
    now = clock.now()
    live = [request for request in found if not request.expired(now)]
    for request in live:
        if request.status is RequestStatus.APPROVED and _served(request.id):
            await _check_maker(guard, maker)
            spent = await _spend(guard, request.id, arguments)
            if spent is not None:
                return Running(
                    spent.id, spent.action, command, spent.maker, spent.required
                )
    pending = [each for each in live if each.status is RequestStatus.PENDING]
    waiting = next((each for each in pending if _served(each.id)), None)
    if waiting is None and pending:
        waiting = pending[0]
    _kept_for(None if waiting is None else waiting.id)
    if waiting is None:
        waiting = await _submit(guard, command, maker, ApprovalsUnitOfWork())
    _keep_authorizations(waiting.id, waiting.expires_at)
    raise ApprovalPending(
        "This command needs approval: it was submitted for approval.", waiting.id
    )


HELD_FOR: Final = "approvals.request:"
"""What auth is told a maker's held call waits on: the request, by id."""


def _keep_authorizations(request_id: UUID, until: datetime) -> None:
    """Keep the action authorization of the maker's held call for the request.

    With ``domainkit.contrib.auth`` installed, its ``hold_authorizations``:
    the call made again once the request ``request_id`` is approved spends
    it, within ``AUTH.AUTHORIZATIONS.HELD_TTL``, while the request still
    waits (``held_request_waits``), until ``until``, its expiry (ADR 0174).
    Without auth, there is none.
    """
    if registry.is_installed(AUTH):
        from domainkit.contrib.auth.actions import hold_authorizations

        hold_authorizations(until, held_for=f"{HELD_FOR}{request_id}")


def _served(request_id: UUID) -> bool:
    """Whether the call's act's authorization may serve the request ``request_id``.

    With ``domainkit.contrib.auth`` installed: unless it is kept for another
    request (auth's ``held_elsewhere``). Without auth, there is none.
    """
    if not registry.is_installed(AUTH):
        return True
    from domainkit.contrib.auth.actions import held_elsewhere

    return not held_elsewhere(f"{HELD_FOR}{request_id}")


def _kept_for(request_id: UUID | None) -> None:
    """Refuse the call if its act's authorization is kept for another request.

    With ``domainkit.contrib.auth`` installed, its ``refuse_held_elsewhere``:
    before the call spends or waits on the request ``request_id``, or
    submits one (None), an authorization kept for another request serves
    that one alone (ADR 0179). Without auth, there is none.

    Raises:
        ActionNotAuthorized: It is kept for another request (auth's, 403).
    """
    if registry.is_installed(AUTH):
        from domainkit.contrib.auth.actions import refuse_held_elsewhere

        refuse_held_elsewhere(None if request_id is None else f"{HELD_FOR}{request_id}")


async def held_request_waits(held_for: str) -> bool | None:
    """Whether the request an authorization is held for waits still (auth's hook).

    Pending or approved, unrun, and its time not up: True. Rejected,
    cancelled, expired, failed, carried out, or gone: False, and the hold
    is over. None for what is not a request of approvals'.
    """
    if not held_for.startswith(HELD_FOR):
        return None
    try:
        request_id = RequestId(UUID(held_for.removeprefix(HELD_FOR)))
    except ValueError:
        return False
    async with ApprovalsUnitOfWork() as uow:
        request = await uow.requests.find(request_id)
    return (
        request is not None
        and request.status in {RequestStatus.PENDING, RequestStatus.APPROVED}
        and not request.expired(clock.now())
    )


async def _spend(
    guard: Guard, request_id: UUID, arguments: object
) -> ApprovalRequest | None:
    """Mark the approved request ``request_id`` executed, if it may run now.

    None if it is no longer approved, has expired, or fails the checks of a
    run (it fails then, with the refusal's code). Its command must read as
    ``arguments``: the request was found by its fingerprint column, which
    its seal does not cover, and the arguments its seal covers are what
    the approvers were shown. The call's ``when_spent`` check runs last,
    under the lock: one that raises leaves the request approved.

    Raises:
        Exception: Whatever the ``when_spent`` check raises.
    """
    async with ApprovalsUnitOfWork() as uow:
        request = await uow.locked(request_id)
        if request.status is not RequestStatus.APPROVED or request.expired(clock.now()):
            return None
        try:
            request.check_sealed()
            if guard.dump(await _rebuild(guard, request)) != arguments:
                raise FingerprintMismatch(
                    "The request holds another command than the one approved."
                )
        except DomainError as exc:
            logger.warning("Approval request %s fails as it is run", request_id)
            request.failed(exc.code)
            await uow.commit()
            return None
        last = when_spent()
        if last is not None:
            await last()  # Under the lock: the one call that spends it.
        request.executed()
        await uow.commit()
    return request


async def approve_request(
    command: ApproveRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Approve a request; the approval that completes it queues its command.

    Under the request's lock, so concurrent approvals count one by one, and
    one of them completes it; that one lock is all they wait for, so they
    never deadlock. The command is queued through the outbox in the same
    transaction (``execute_request``). With ``APPROVALS.APPROVE_ACTION``
    on, it spends the approver's authorization of this approval
    (``approvals.approve``), with the approval's commit where the requests
    share auth's database: called without one, it is refused.

    Raises:
        RequestNotFound: No request has that id.
        RequestTampered: Its seal does not hold.
        PolicyRemoved: Its guard or policy no longer exists.
        RequestNotPending: It is decided already, or has expired.
        MakerCannotDecide: The approver submitted it.
        InterestedParty: The command is about the approver.
        AlreadyDecided: The approver decided it already.
        FingerprintMismatch: ``fingerprint`` is not the request's.
        AuthorizationRequired: With ``APPROVE_ACTION``, no authorization
            of this approval came with the request (auth's, 401).
        ActionNotAuthorized: With ``APPROVE_ACTION``, it is of another
            request or fingerprint (auth's, 403).
    """
    if config().APPROVE_ACTION:
        from .authorizing import approve_authorized

        return await approve_authorized(command, uow)
    return await record_approval(command, uow)


async def record_approval(
    command: ApproveRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Approve a request, asking for no authorization: for the project's own code.

    What ``approve_request`` does, whatever ``APPROVALS.APPROVE_ACTION``
    says, refusing all it refuses: the maker, a party to the command, a
    second decision, another fingerprint, a request no longer pending, or
    changed in the database. The approval that completes the request
    queues its command. It commits as the request's tenant alone, inside
    ``all_tenants()`` too, so what the commit publishes and queues is that
    tenant's. Call it from code the project trusts, never from a route a
    user reaches. It trusts its caller for who acts: the approver
    is not asked to be one the policy names, nor to pass the approve rule,
    which the route asks. Code that names a person it did not authenticate
    checks them first (``approvers_of``).

    Raises:
        RequestNotFound: As for ``approve_request``.
        RequestTampered: As for ``approve_request``.
        PolicyRemoved: As for ``approve_request``.
        RequestNotPending: As for ``approve_request``.
        MakerCannotDecide: As for ``approve_request``.
        InterestedParty: As for ``approve_request``.
        AlreadyDecided: As for ``approve_request``.
        FingerprintMismatch: As for ``approve_request``.
    """
    approver = _person(command.approver, APPROVER)

    async with uow:
        request = await uow.locked(command.request_id)
        request.check_sealed()
        parties = await _parties(request)
        if request.approve(approver, command.fingerprint, parties) and not (
            policy_of(request)[0].by_maker
        ):
            uow.defer(execute_request, request_id=request.id, token=request.run_token)
        with tenant_alone(request.tenant_id):  # Code across tenants included.
            await uow.commit()
    return request


async def check_approval(command: ApproveRequest) -> ApprovalRequest:
    """The request ``command`` approves, if the approver may approve it now.

    What approving checks, changing nothing: asking for an authorization
    of an approval that would be refused is refused as it would be.

    Raises:
        RequestNotFound: As for ``approve_request``.
        RequestTampered: As for ``approve_request``.
        PolicyRemoved: As for ``approve_request``.
        RequestNotPending: As for ``approve_request``.
        MakerCannotDecide: As for ``approve_request``.
        InterestedParty: As for ``approve_request``.
        AlreadyDecided: As for ``approve_request``.
        FingerprintMismatch: As for ``approve_request``.
    """
    approver = _person(command.approver, APPROVER)
    request = await _read(command.request_id)
    request.check_sealed()
    request.check_decision(approver, command.fingerprint, await _parties(request))
    return request


async def _parties(request: ApprovalRequest) -> frozenset[str]:
    """The people the request's command is about, who may not decide it.

    Raises:
        PolicyRemoved: The request's guard or policy no longer exists.
        InvalidArguments: Its arguments no longer make its command.
    """
    guard, _ = policy_of(request)
    try:
        command = guard.load(request.arguments)
    except ValidationError:
        raise InvalidArguments("The arguments do not make the command.") from None
    return await guard.aparties_of(command)


async def reject_request(
    command: RejectRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Reject a request for a reason; one rejection ends it.

    With ``APPROVALS.REJECT_ACTION`` on, it spends the approver's
    authorization of this rejection (``approvals.reject``), as approving
    spends one: called without one, it is refused.

    Raises:
        RequestNotFound: As for ``approve_request``.
        RequestTampered: As for ``approve_request``.
        PolicyRemoved: As for ``approve_request``.
        RequestNotPending: As for ``approve_request``.
        MakerCannotDecide: As for ``approve_request``.
        InterestedParty: As for ``approve_request``.
        AlreadyDecided: As for ``approve_request``.
        InvalidArguments: The reason is longer than 500 characters.
        AuthorizationRequired: With ``REJECT_ACTION``, no authorization of
            this rejection came with the request (auth's, 401).
        ActionNotAuthorized: With ``REJECT_ACTION``, it is of another
            request or reason (auth's, 403).
    """
    if config().REJECT_ACTION:
        from .authorizing import reject_authorized

        return await reject_authorized(command, uow)
    return await record_rejection(command, uow)


async def record_rejection(
    command: RejectRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Reject a request, asking for no authorization: for the project's own code.

    What ``reject_request`` does, whatever ``APPROVALS.REJECT_ACTION``
    says, refusing all it refuses. Call it from code the project trusts,
    never from a route a user reaches. It trusts its caller for who acts:
    the approver is not asked to be one the policy names, which the route
    asks.

    Raises:
        RequestNotFound: As for ``reject_request``.
        RequestTampered: As for ``reject_request``.
        PolicyRemoved: As for ``reject_request``.
        RequestNotPending: As for ``reject_request``.
        MakerCannotDecide: As for ``reject_request``.
        InterestedParty: As for ``reject_request``.
        AlreadyDecided: As for ``reject_request``.
        InvalidArguments: As for ``reject_request``.
    """
    approver = _person(command.approver, APPROVER)
    reason = _reason(command.reason)

    async with uow:
        request = await uow.locked(command.request_id)
        request.check_sealed()
        request.reject(approver, reason, await _parties(request))
        with tenant_alone(request.tenant_id):
            await uow.commit()
    return request


async def cancel_request(
    command: CancelRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Withdraw a pending request, as its maker.

    With ``APPROVALS.CANCEL_ACTION`` on, it spends the maker's
    authorization of this cancellation (``approvals.cancel``), as
    approving spends one: called without one, it is refused.

    Raises:
        RequestNotFound: No request has that id.
        RequestTampered: Its seal does not hold.
        RequestNotPending: It is decided already.
        NotTheMaker: ``maker`` did not submit it.
        InvalidArguments: The reason is longer than 500 characters.
        AuthorizationRequired: With ``CANCEL_ACTION``, no authorization of
            this cancellation came with the request (auth's, 401).
        ActionNotAuthorized: With ``CANCEL_ACTION``, it is of another
            request or reason (auth's, 403).
    """
    if config().CANCEL_ACTION:
        from .authorizing import cancel_authorized

        return await cancel_authorized(command, uow)
    return await record_cancellation(command, uow)


async def record_cancellation(
    command: CancelRequest, uow: ApprovalsUnitOfWork
) -> ApprovalRequest:
    """Withdraw a request, asking for no authorization: for the project's own code.

    What ``cancel_request`` does, whatever ``APPROVALS.CANCEL_ACTION``
    says: only for the request's maker, while it is pending. Call it from
    code the project trusts (a task that withdraws stale requests, a
    handler that withdraws one whose subject is gone), never from a route
    a user reaches.

    Raises:
        RequestNotFound: As for ``cancel_request``.
        RequestTampered: As for ``cancel_request``.
        RequestNotPending: As for ``cancel_request``.
        NotTheMaker: As for ``cancel_request``.
        InvalidArguments: As for ``cancel_request``.
    """
    reason = _reason(command.reason)

    async with uow:
        request = await uow.locked(command.request_id)
        request.cancel(command.maker, reason)
        with tenant_alone(request.tenant_id):
            await uow.commit()
    return request


async def check_rejection(command: RejectRequest) -> ApprovalRequest:
    """The request ``command`` rejects, if the approver may reject it now.

    What rejecting checks, changing nothing.

    Raises:
        RequestNotFound: As for ``reject_request``.
        RequestTampered: As for ``reject_request``.
        PolicyRemoved: As for ``reject_request``.
        RequestNotPending: As for ``reject_request``.
        MakerCannotDecide: As for ``reject_request``.
        InterestedParty: As for ``reject_request``.
        AlreadyDecided: As for ``reject_request``.
        InvalidArguments: As for ``reject_request``.
    """
    approver = _person(command.approver, APPROVER)
    _reason(command.reason)
    request = await _read(command.request_id)
    request.check_sealed()
    request.check_decision(approver, request.fingerprint, await _parties(request))
    return request


async def check_cancellation(command: CancelRequest) -> ApprovalRequest:
    """The request ``command`` withdraws, if its maker may withdraw it now.

    What cancelling checks, changing nothing.

    Raises:
        RequestNotFound: As for ``cancel_request``.
        RequestTampered: As for ``cancel_request``.
        RequestNotPending: As for ``cancel_request``.
        NotTheMaker: As for ``cancel_request``.
        InvalidArguments: As for ``cancel_request``.
    """
    _reason(command.reason)
    request = await _read(command.request_id)
    request.check_cancel(command.maker)
    return request


async def expire_requests() -> int:
    """Expire the pending requests whose time is up; how many there were.

    Every tenant's, each read, expired and committed as its tenant alone,
    so what its expiry publishes, and the handlers that hear it, are that
    tenant's. One whose seal does not hold, or whose stored tenant is not
    a tenant id, is left as it is, and logged: expiring it would seal what
    someone changed around the application. One gone meanwhile is passed
    over.
    """
    expired = 0
    left: set[RequestId] = set()
    while True:
        async with ApprovalsUnitOfWork() as uow:
            due = await uow.requests.due(clock.now(), EXPIRE_BATCH)
            tenants = await uow.requests.tenants_of(due)
        for request_id in due:
            tenant_id = tenants.get(request_id)
            if not servable(tenant_id):
                logger.warning("Approval request %s is tampered with", request_id)
                left.add(request_id)
                continue
            with tenant_alone(tenant_id):
                async with ApprovalsUnitOfWork() as uow:
                    try:
                        request = await uow.locked(request_id)
                    except RequestNotFound:  # Gone since it was found due.
                        left.add(request_id)
                        continue
                    if request.status is not RequestStatus.PENDING:
                        continue
                    try:
                        request.expire()
                    except RequestTampered:
                        logger.warning(
                            "Approval request %s is tampered with", request_id
                        )
                        left.add(request_id)
                        continue
                    await uow.commit()
                    expired += 1
        if len(due) < EXPIRE_BATCH or set(due) <= left:
            return expired


async def _rebuild(guard: Guard, request: ApprovalRequest) -> object:
    """The approved command, if it is what was approved.

    Raises:
        InvalidArguments: The arguments no longer make the command, or make
            one that reads back otherwise (the command changed meanwhile).
        FingerprintMismatch: Fewer people than needed approved what it
            holds, by decisions whose seals hold, other than its maker and
            the people the command is about.
    """
    try:
        command = guard.load(request.arguments)
    except ValidationError:
        raise InvalidArguments("The arguments do not make the command.") from None
    if guard.dump(command) != request.arguments:
        raise InvalidArguments(
            "The command reads otherwise now than when it was approved."
        )
    if not guard.names_its_maker(command, request.maker):
        raise InvalidArguments(
            "The command names someone other than its maker as the one who acts."
        )
    if request.approvals_verified(await guard.aparties_of(command)) < request.required:
        raise FingerprintMismatch(
            "The request holds another command than the one approved."
        )
    return command


async def _check_maker(guard: Guard, maker: str) -> None:
    """Refuse a run for a maker who may no longer make the command (FIN-93).

    With ``domainkit.contrib.auth`` installed, a maker that is a user's id
    must still be an active user, holding the guard's maker permission if
    it names one and passing its ``maker_rule`` if it has one (SEC-16b-7):
    a clerk dismissed, a customer frozen, or staff demoted after
    submitting runs nothing. It is asked in the request's tenant, where
    the run is (a guard ``everywhere``: as where no tenant is served).
    The rule sees the user on a call of its own, outside any request,
    with no credentials (``OfflineContext``). Rules about scopes were
    checked when the request was submitted over HTTP; here no scopes are
    found, which is no limit, so they do not limit. A step-up refuses
    here, as does a rule that raises an error of its own (``_allows``). A
    ``DomainError`` it raises fails the run with its code, as in 2.8, and
    a transient error is raised for the run to be tried again. A maker
    that names no user (a principal of the project's own) is left to the
    project.

    Raises:
        MakerNotAllowed: The user is gone, inactive, lacks the permission,
            or fails the maker rule.
        DomainError: The maker rule raised one.
    """
    if not registry.is_installed(AUTH):
        return
    try:
        user_id = UUID(maker)
    except ValueError:
        return
    from domainkit.contrib.auth import get_user, has_perm

    user = await get_user(user_id)
    rule = rule_instance(guard.maker_rule)
    with guard.checking():  # A guard of rows of no tenant: no role counts.
        allowed = user is not None and (
            (guard.permission is None or await has_perm(user, guard.permission))
            and (rule is None or await _allows(rule, user, raised=(DomainError,)))
        )
    if not allowed:
        raise MakerNotAllowed("Whoever submitted this command may no longer make it.")


async def _allows(
    rule: BasePermission,
    user: Any,  # noqa: ANN401 - a user.
    *,
    raised: tuple[type[Exception], ...] = (),
) -> bool:
    """Whether ``rule`` allows ``user`` on a call of their own, outside any request.

    Each is asked on a new ``OfflineContext``, a request context of its
    own. A transient error (``transient``: a timeout, a connection lost, a
    backend or a database out of reach) and the errors of ``raised`` go
    up, for the caller to try again or to answer. A rule that raises any
    other refuses: it is logged by its class and the error's, never the
    error's text, and reported (``report_error``), as a run that failed
    was in 2.8, so the mistake still reaches whoever watches the errors. A
    run it would fail is not tried again for it, nor is a list of
    approvers lost.
    """
    try:
        return await rule.has_permission(
            OfflineContext(user, method="POST", path=f"/{EXECUTE}"), user
        )
    except Exception as exc:
        if transient(exc) or isinstance(exc, raised):
            raise
        name = type(rule).__qualname__
        rule_log.warning(
            "rule_raised",
            name,
            "The access rule %s, asked outside a request, raised %s: it refuses",
            name,
            type(exc).__name__,
        )
        report_error(exc, rule=name)
        return False


async def approvers_of(request: ApprovalRequest) -> list[Any]:
    """The active users who may decide ``request`` now: whom to tell of it.

    It needs ``domainkit.contrib.auth``. They are, by the request's policy,
    the users who hold its permission (``has_perm``: granted to them or to
    a group of theirs, and superusers), the members of its group, or the
    superusers and staff its approver rule lets through, asked about the
    user alone, on a call without credentials (``OfflineContext``); a rule
    that raises leaves the user out, and is logged, but for a transient
    error, which is raised. Never its
    maker, a party to its command, or someone who decided it already. A
    permission only a backend of the project's grants is not looked for:
    such holders are not found (user report P3). They are asked as the
    request's tenant, where it is decided (ADR 0183).

    Raises:
        ImproperlyConfigured: ``domainkit.contrib.auth`` is not installed.
        PolicyRemoved: The request's guard or policy no longer exists.
    """
    if not registry.is_installed(AUTH):
        raise ImproperlyConfigured(f"approvers_of needs {AUTH}: install it.")
    with tenant_alone(request.tenant_id):  # Who may decide it, where it is.
        return await _approvers(request)


async def _approvers(request: ApprovalRequest) -> list[Any]:
    """``approvers_of``'s work, as the request's tenant.

    Raises:
        PolicyRemoved: The request's guard or policy no longer exists.
    """
    from domainkit.contrib.auth import AuthUnitOfWork, get_user_model, has_perm

    guard, policy = policy_of(request)
    left_out = {request.maker, *await _parties(request)}
    left_out |= {each.approver for each in request.decisions}
    if request.permission is not None:
        codename = request.permission.partition(".")[2]
        found: Criteria = (
            (F("is_superuser") == True)  # noqa: E712 - a condition.
            | (F("user_permissions.codename") == codename)
            | (F("groups.permissions.codename") == codename)
        )
    elif request.group is not None:
        found = F("groups.name") == request.group
    else:
        stored = {each.name for each in fields(get_user_model())}
        flags = [F(name) == True for name in STAFF_FLAGS if name in stored]  # noqa: E712
        found = functools.reduce(operator.or_, flags)
    async with AuthUnitOfWork() as uow:
        users = await uow.users.filter(
            Selection((F("is_active") == True) & found)  # noqa: E712 - a condition.
        )
    rule = rule_instance(policy.approver_rule)
    approvers = []
    with guard.checking():
        for user in users:
            if str(user.id) in left_out:
                continue
            if request.permission is not None:
                if not await has_perm(user, request.permission):
                    continue
            elif request.group is None and (
                rule is None or not await _allows(rule, user)
            ):
                continue
            approvers.append(user)
    return approvers


async def _read(request_id: UUID) -> ApprovalRequest:
    """The request ``request_id``, as committed (``RequestNotFound`` if none)."""
    async with ApprovalsUnitOfWork() as uow:
        return await uow.requests.get(RequestId(request_id))


async def _finish(request_id: UUID, token: str, error: str | None = None) -> None:
    """Mark the request executed, or failed with ``error``, if ``token`` runs it."""
    async with ApprovalsUnitOfWork() as uow:
        request = await uow.locked(request_id)
        if request.runs(token):
            if error is None:
                request.executed()
            else:
                request.failed(error)
            await uow.commit()


async def _fail_tampered(request_id: UUID) -> None:
    """Fail an approved request whose seal does not hold: it never runs.

    Its tenant is not to be trusted either: the failure is committed as no
    tenant.
    """
    with all_tenants():
        async with ApprovalsUnitOfWork() as uow:
            request = await uow.locked(request_id)
            if request.status is RequestStatus.APPROVED and not request.sealed():
                logger.warning("Approval request %s is tampered with", request_id)
                request.failed(RequestTampered.code)
                with tenant_alone(None):
                    await uow.commit()


@contextmanager
def _as_maker(maker: str) -> Iterator[None]:
    """Act as ``maker`` in the block: what the command does is theirs.

    The approvers are named by the request's own events (``ApprovalGiven``,
    and ``RequestExecuted``'s ``approvers``).
    """
    current = current_request()
    if current is None:
        current = RequestContext(
            request_id=uuid.uuid4().hex, client_ip=None, method="TASK", path=EXECUTE
        )
    with bind_request(replace(current, actor=maker, impersonating=None)):
        yield


class _Marking:
    """A guarded use case's unit of work, which marks the request executed with it.

    For a unit of work on the requests' database: its first commit locks
    the request, marks it executed (``RequestExecuted``), spending its run
    token, and commits that with the use case's change, so neither commits
    without the other. Everything else is the unit of work's own.
    """

    def __init__(self, uow: SqlAlchemyUnitOfWork, request_id: UUID, token: str) -> None:
        """Wrap ``uow``, for the request ``request_id`` run by ``token``."""
        self._uow = uow
        self._request_id = request_id
        self._token = token
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
            RequestNotPending: The token no longer runs the request (it ran).
        """
        if not self.marked:
            await self._uow.lock(f"approvals.request:{self._request_id}")
            repository: RequestRepository = self._uow.approval_requests  # type: ignore[attr-defined]
            request = await repository.get(RequestId(self._request_id))
            if not request.runs(self._token):
                raise RequestNotPending("The request is not waiting to run.")
            request.executed()
        await self._uow.commit()
        self.marked = True


def _marking(uow: object, request_id: UUID, token: str) -> "_Marking | None":
    """``uow``, to mark the request with its commit, if on the requests' database."""
    if isinstance(uow, SqlAlchemyUnitOfWork) and uow.alias == approvals_alias():
        return _Marking(uow, request_id, token)
    return None


async def execute_request(request_id: UUID, token: str | None = None) -> None:
    """Run an approved request's command, once, as its maker (the task ``EXECUTE``).

    Only the message its approval queued runs it: ``token`` must be the
    request's run token, which the run spends. A request that is not
    approved (it ran, or failed), or a token that is not its own (a message
    queued again), runs nothing; a request whose seal does not hold fails
    (``request_tampered``). A command that is not what was approved, whose
    guard or policy is gone, or whose maker may no longer make it
    (``maker_not_allowed``: FIN-93) fails the request, and so does a
    refusal of the use case's (a ``DomainError`` other than
    ``ConcurrentUpdate``), with the refusal's code. Anything else is
    raised, for the worker to retry.

    It runs in the request's own tenant alone (none for a request of
    none), whichever tenant the message names: the command, the maker's
    check, and the request's end are that tenant's. The seal is checked
    first, so a tenant changed around the application is never served
    (ADR 0183).
    """
    with all_tenants():  # Only to learn its tenant: nothing runs in here.
        request = await _read(request_id)
    if request.status is not RequestStatus.APPROVED:
        return
    if not request.sealed():
        await _fail_tampered(request_id)
        return
    with tenant_alone(request.tenant_id):
        await _execute(request, token)


async def _execute(request: ApprovalRequest, token: str | None) -> None:
    """``execute_request``'s work, in the request's tenant: approved and sealed."""
    from domainkit.contrib.messaging import AlreadyHandled

    request_id = request.id
    if token is None or not request.runs(token):
        logger.warning(
            "A message to run approval request %s holds no run token of it", request_id
        )
        return
    with _as_maker(request.maker):
        try:
            guard, _ = policy_of(request)
            if guard.by_maker:  # Its maker's call runs it, never the worker.
                return
            command = await _rebuild(guard, request)
            await _check_maker(guard, request.maker)
        except DomainError as exc:
            await _finish(request_id, token, exc.code)
            return
        uow = guard.unit_of_work()
        marking = _marking(uow, request_id, token)
        try:
            with running(
                request.id, request.action, command, request.maker, request.required
            ):
                await guard(command, marking or uow)
        except AlreadyHandled:  # It committed in another database, then the run ended.
            await _finish(request_id, token)
            raise
        except ConcurrentUpdate:
            raise
        except (DomainError, MoneyError) as exc:
            await _finish(request_id, token, exc.code)
            return
        if marking is None or not marking.marked:
            await _finish(request_id, token)
