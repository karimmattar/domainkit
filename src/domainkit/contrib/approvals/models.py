"""Approval requests: a command waiting for its approvals, and who decided.

A request holds one command of a guarded use case, as JSON, with its
fingerprint (``fingerprint``: SHA-256 of its action and arguments), the
maker who submitted it, and the policy it waits under: how many approvals
it needs, and who may give them. Each approval or rejection is a
``Decision`` of one person, about that fingerprint. The request is
approved once it has as many approvals as its policy asks, rejected by
one rejection, cancelled by its maker, or expired; then the approved
command runs once, and the request is executed, or failed.

The request and each decision carry a ``seal``: an HMAC under a key
derived from ``SECRET_KEY`` of what they say. The request's covers its
id, action, fingerprint, maker, policy, approvals needed, approvers
(permission or group), expiry, status, and run token, and is made again
with each change of its state; a decision's covers its request, person,
verdict, and fingerprint. Every change and every run checks the seals
first, so a row changed or added around the application, by someone
without the key, is neither decided nor run (``RequestTampered``). An
approved request holds a one-time ``run_token``, which the message that
runs it carries and the run spends, so a message queued again, or a
status set back, runs nothing.
"""

import hashlib
import json
import secrets
from collections.abc import Collection
from dataclasses import field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, NewType, Self
from uuid import UUID, uuid4

from domainkit import clock, signing
from domainkit.ddd import AggregateRoot, DomainEvent, Entity, MaxLength

from .exceptions import (
    AlreadyDecided,
    FingerprintMismatch,
    InterestedParty,
    MakerCannotDecide,
    NotTheMaker,
    RequestNotPending,
    RequestTampered,
)

RequestId = NewType("RequestId", UUID)
DecisionId = NewType("DecisionId", UUID)

MAX_ACTION: Final = 128
MAX_PERSON: Final = 255
"""The longest action name, and the longest id of a maker or an approver."""
MAX_REASON: Final = 500
"""The longest reason given for a rejection or a cancellation."""
SEAL_SALT: Final = "domainkit.contrib.approvals.seal"
"""The purpose the seals' key is derived for."""


class RequestStatus(StrEnum):
    """Where a request is: waiting, decided, or run."""

    PENDING = "pending"
    APPROVED = "approved"
    """Approved, and its command queued to run."""
    EXECUTED = "executed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    """Approved, but its command was refused when it ran (``reason`` says why)."""


class Verdict(StrEnum):
    """What one person decided about a request."""

    APPROVE = "approve"
    REJECT = "reject"


class RequestSubmitted(DomainEvent):
    """A maker submitted a command for approval."""

    request_id: RequestId
    action: str
    policy: str
    fingerprint: str
    required: int


class ApprovalGiven(DomainEvent):
    """One person approved a request; ``approvals`` of ``required`` so far."""

    request_id: RequestId
    fingerprint: str
    approvals: int
    required: int


class RequestApproved(DomainEvent):
    """A request has its approvals; its command is queued to run."""

    request_id: RequestId
    action: str


class RequestRejected(DomainEvent):
    """One person rejected a request, with a reason (kept on the request)."""

    request_id: RequestId


class RequestCancelled(DomainEvent):
    """The maker withdrew a request."""

    request_id: RequestId


class RequestExpired(DomainEvent):
    """A request's time ran out before its approvals came."""

    request_id: RequestId


class RequestExecuted(DomainEvent):
    """An approved command ran, as its maker; ``approvers`` approved it."""

    request_id: RequestId
    action: str
    approvers: tuple[str, ...] = ()


class RequestFailed(DomainEvent):
    """An approved command was refused when it ran; ``error`` is the refusal's code."""

    request_id: RequestId
    action: str
    error: str


def fingerprint(action: str, arguments: dict[str, Any]) -> str:
    """SHA-256 of the action and the arguments, as canonical JSON (64 hex digits).

    Keys sorted, no spaces, so the same command always gives the same
    fingerprint, and an approver's client can work it out from what it shows.
    """
    text = json.dumps(
        {"action": action, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seal_of(*parts: object) -> str:
    """The seal of ``parts``: an HMAC-SHA256 under ``SECRET_KEY``, for approvals."""
    return signing.Signer(salt=SEAL_SALT).signature(_sealed_text(parts))


def seal_holds(value: str, *parts: object) -> bool:
    """Whether ``value`` is the seal of ``parts``, under a current or fallback key."""
    signer = signing.Signer(salt=SEAL_SALT)
    try:
        signer.unsign(f"{_sealed_text(parts)}:{value}")
    except signing.BadSignature:
        return False
    return True


def _sealed_text(parts: tuple[object, ...]) -> str:
    """``parts`` as one text: a JSON list of their texts, so no two lists read alike."""
    return json.dumps([str(part) for part in parts], separators=(",", ":"))


class Decision(Entity[DecisionId]):
    """One person's approval or rejection of a request, and what they saw."""

    class Meta:
        """Decisions are read with their request: no permissions of their own."""

        default_permissions = ()

    approver: Annotated[str, MaxLength(MAX_PERSON)]
    verdict: Verdict
    fingerprint: Annotated[str, MaxLength(64)]
    seal: Annotated[str, MaxLength(128)]
    decided_at: datetime

    def verified(self, request_id: "RequestId") -> bool:
        """Whether its seal is that of what it says, about ``request_id``."""
        return seal_holds(
            self.seal, request_id, self.approver, self.verdict.value, self.fingerprint
        )


class ApprovalRequest(AggregateRoot[RequestId]):
    """A command that runs only once enough people other than its maker approve it."""

    class Meta:
        """Its permissions: viewing and submitting; deciding is by policy."""

        default_permissions = ("view", "add")

    action: Annotated[str, MaxLength(MAX_ACTION)]
    arguments: dict[str, Any]
    fingerprint: Annotated[str, MaxLength(64)]
    seal: Annotated[str, MaxLength(128)]
    maker: Annotated[str, MaxLength(MAX_PERSON)]
    policy: Annotated[str, MaxLength(MAX_ACTION)]
    required: int
    approvals: int = 0
    """How many people approved it so far: stored, so each approval changes
    the request's row, and its version, as well as adding a decision."""
    permission: Annotated[str, MaxLength(MAX_PERSON)] | None = None
    group: Annotated[str, MaxLength(MAX_PERSON)] | None = None
    status: RequestStatus = RequestStatus.PENDING
    reason: Annotated[str, MaxLength(MAX_REASON)] = ""
    submitted_at: datetime = field(default_factory=clock.now)
    expires_at: datetime
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    run_token: Annotated[str, MaxLength(64)] | None = None
    """Set when it is approved, carried by the message that runs it, spent
    by the run: a one-time key to running it."""
    decisions: list[Decision] = field(default_factory=list)

    @classmethod
    def submit(
        cls,
        action: str,
        arguments: dict[str, Any],
        *,
        maker: str,
        policy: str,
        required: int,
        permission: str | None,
        group: str | None,
        expires_at: datetime,
    ) -> Self:
        """A pending request for ``arguments``, sealed; records ``RequestSubmitted``."""
        request_id = RequestId(uuid4())
        digest = fingerprint(action, arguments)
        request = cls(
            id=request_id,
            action=action,
            arguments=arguments,
            fingerprint=digest,
            seal="",
            maker=maker,
            policy=policy,
            required=required,
            permission=permission,
            group=group,
            expires_at=expires_at,
        )
        request._reseal()
        request.record(
            RequestSubmitted(
                request_id=request_id,
                action=action,
                policy=policy,
                fingerprint=digest,
                required=required,
            )
        )
        return request

    def _state(self) -> tuple[object, ...]:
        """What its seal covers: everything but the reason and the times decided."""
        return (
            self.id,
            self.action,
            fingerprint(self.action, self.arguments),
            self.maker,
            self.policy,
            self.required,
            self.permission,
            self.group,
            self.expires_at.isoformat(),
            self.status.value,
            self.run_token,
        )

    def _reseal(self) -> None:
        """Seal what it holds now (after a change the application made)."""
        self.seal = seal_of(*self._state())

    def sealed(self) -> bool:
        """Whether its seal is that of what it holds now."""
        return seal_holds(self.seal, *self._state())

    def check_sealed(self) -> None:
        """Refuse a request changed outside the application.

        Raises:
            RequestTampered: Its seal does not hold.
        """
        if not self.sealed():
            raise RequestTampered(
                "The request was changed outside the application; it is neither "
                "decided nor run."
            )

    def approvals_verified(self, parties: Collection[str] = ()) -> int:
        """How many people approved what it holds, by seals that hold.

        0 if its own seal does not hold. Only approvals of its fingerprint,
        by people other than its maker and ``parties``, once each, count.
        """
        if not self.sealed():
            return 0
        return len(
            {
                each.approver
                for each in self.decisions
                if each.verdict is Verdict.APPROVE
                and each.approver != self.maker
                and each.approver not in parties
                and each.fingerprint == self.fingerprint
                and each.verified(self.id)
            }
        )

    def approvers(self) -> tuple[str, ...]:
        """Who approved it, in the order they did."""
        return tuple(
            each.approver for each in self.decisions if each.verdict is Verdict.APPROVE
        )

    def expired(self, now: datetime) -> bool:
        """Whether its time is up at ``now``."""
        return self.expires_at <= now

    def _decide(
        self, person: str, verdict: Verdict, digest: str, parties: Collection[str]
    ) -> datetime:
        """Add ``person``'s decision about ``digest``; when it was taken.

        Raises:
            RequestTampered: Its seal does not hold.
            RequestNotPending: It is decided already, or its time is up.
            MakerCannotDecide: ``person`` submitted it.
            InterestedParty: The command is about ``person``.
            AlreadyDecided: ``person`` decided it already.
            FingerprintMismatch: ``digest`` is not what it holds.
        """
        self.check_sealed()
        now = clock.now()
        if self.status is not RequestStatus.PENDING:
            raise RequestNotPending("The request is no longer waiting for a decision.")
        if self.expired(now):
            raise RequestNotPending("The request has expired.")
        if person == self.maker:
            raise MakerCannotDecide("Whoever submitted a request cannot decide it.")
        if person in parties:
            raise InterestedParty("Whoever the command is about cannot decide it.")
        if any(each.approver == person for each in self.decisions):
            raise AlreadyDecided("You have decided this request already.")
        if digest != self.fingerprint:
            raise FingerprintMismatch(
                "The request holds another command than the one decided on."
            )
        self.decisions.append(
            Decision(
                id=DecisionId(uuid4()),
                approver=person,
                verdict=verdict,
                fingerprint=digest,
                seal=seal_of(self.id, person, verdict.value, digest),
                decided_at=now,
            )
        )
        return now

    def approve(
        self, approver: str, digest: str, parties: Collection[str] = ()
    ) -> bool:
        """Add ``approver``'s approval of ``digest``; whether that approved it.

        Records ``ApprovalGiven``, and ``RequestApproved`` with the last one,
        which gives it a new ``run_token``.

        Raises:
            RequestTampered, RequestNotPending, MakerCannotDecide,
                InterestedParty, AlreadyDecided, FingerprintMismatch: As a
                decision is refused.
        """
        now = self._decide(approver, Verdict.APPROVE, digest, parties)
        self.approvals = approvals = self.approvals + 1
        self.record(
            ApprovalGiven(
                request_id=self.id,
                fingerprint=digest,
                approvals=approvals,
                required=self.required,
            )
        )
        if approvals < self.required:
            return False
        self.status = RequestStatus.APPROVED
        self.decided_at = now
        self.run_token = secrets.token_hex(16)
        self._reseal()
        self.record(RequestApproved(request_id=self.id, action=self.action))
        return True

    def reject(self, approver: str, reason: str, parties: Collection[str] = ()) -> None:
        """Reject it for ``reason``; records ``RequestRejected``.

        Raises:
            RequestTampered, RequestNotPending, MakerCannotDecide,
                InterestedParty, AlreadyDecided: As a decision is refused.
        """
        self.decided_at = self._decide(
            approver, Verdict.REJECT, self.fingerprint, parties
        )
        self.status = RequestStatus.REJECTED
        self.reason = reason
        self._reseal()
        self.record(RequestRejected(request_id=self.id))

    def cancel(self, maker: str, reason: str) -> None:
        """Withdraw it, as its maker; records ``RequestCancelled``.

        Raises:
            RequestTampered: Its seal does not hold.
            RequestNotPending: It is decided already.
            NotTheMaker: ``maker`` did not submit it.
        """
        self.check_sealed()
        if self.status is not RequestStatus.PENDING:
            raise RequestNotPending("The request is no longer waiting for a decision.")
        if maker != self.maker:
            raise NotTheMaker("Only whoever submitted a request can cancel it.")
        self.status = RequestStatus.CANCELLED
        self.decided_at = clock.now()
        self.reason = reason
        self._reseal()
        self.record(RequestCancelled(request_id=self.id))

    def expire(self) -> None:
        """Mark it expired, if it is still pending; records ``RequestExpired``.

        Raises:
            RequestTampered: Its seal does not hold.
        """
        self.check_sealed()
        if self.status is RequestStatus.PENDING:
            self.status = RequestStatus.EXPIRED
            self.decided_at = clock.now()
            self._reseal()
            self.record(RequestExpired(request_id=self.id))

    def runs(self, token: str | None) -> bool:
        """Whether it is approved, sealed, and ``token`` is its run token."""
        return (
            self.status is RequestStatus.APPROVED
            and self.sealed()
            and token is not None
            and secrets.compare_digest(token, self.run_token or "")
        )

    def executed(self) -> None:
        """Record that its command ran (``RequestExecuted``); spend its run token."""
        self.status = RequestStatus.EXECUTED
        self.executed_at = clock.now()
        self.run_token = None
        self._reseal()
        self.record(
            RequestExecuted(
                request_id=self.id, action=self.action, approvers=self.approvers()
            )
        )

    def failed(self, error: str) -> None:
        """Record that its approved command was refused, by the code ``error``.

        A request whose seal does not hold may fail too: failing is the end,
        and runs nothing.
        """
        self.status = RequestStatus.FAILED
        self.executed_at = clock.now()
        self.reason = error
        self.run_token = None
        self._reseal()
        self.record(RequestFailed(request_id=self.id, action=self.action, error=error))
