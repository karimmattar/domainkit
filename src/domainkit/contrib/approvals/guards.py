"""Guarded use cases: which commands need approval, under which policies.

A use case is guarded in two steps. Its service is decorated, so calling
it directly is refused unless an approved request runs it::

    @requires_approval("payments.refund")
    async def refund(command: Refund, uow: PaymentsUnitOfWork) -> Payment: ...

and its domain registers the guard in ``ready()`` (ADR 0101), with the
policies that decide who approves it and when::

    guard(
        "payments.refund",
        refund,
        unit_of_work=PaymentsUnitOfWork,
        policies=[
            Policy("payments.large_refund", approvals=2,
                   permission="payments.approve_refund",
                   when=lambda command: command.amount > Money.of("1000", "USD")),
        ],
        permission="payments.add_refund",
    )

The first policy whose ``when`` holds for a command applies; when none
does, the command needs no approval and runs at once. A guarded service
called directly with a command that needs approval raises
``ApprovalRequired`` (fail closed), or, for a guard with ``submit=True``,
submits it and raises ``ApprovalPending`` with the request's id. Only
the run of an approved request (``running``) passes, and only for the
command it approved.
"""

import functools
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final, get_type_hints

from pydantic import TypeAdapter

from domainkit.apps.hooks import HookPoint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import BasePermission, Rule
from domainkit.money import Money, MoneyError

from .exceptions import ApprovalPending, ApprovalRequired, UnknownAction
from .settings import LONGEST_WAIT, MAX_APPROVALS

type Service = Callable[[Any, Any], Awaitable[Any]]
"""An application service: ``service(command, uow)``."""

ACTION_MARK: Final = "__approval_action__"
"""The attribute ``requires_approval`` sets on the service it guards."""
NAME_PATTERN: Final = r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+"


def _checked_name(name: str, what: str) -> str:
    """``name``, if it is dotted lower-case words of at most 128 characters."""
    if not isinstance(name, str) or not (
        len(name) <= 128 and re.fullmatch(NAME_PATTERN, name)
    ):
        raise ImproperlyConfigured(
            f"{what} is dotted lower-case words ('payments.refund'), at most "
            f"128 characters: {name!r}."
        )
    return name


@dataclass(frozen=True, slots=True)
class Policy:
    """Who approves a command, how many of them, and for how long it waits.

    Approvers are whoever holds ``permission``, or the members of
    ``group`` (a principal with ``groups``, such as ``contrib.auth``'s
    users): one of the two. The maker never counts, even in the group,
    and one person counts once. ``approve_rule`` is an access rule
    whoever approves must pass as well (a step-up rule, say); without it,
    ``APPROVALS.APPROVE_RULE`` applies. ``when`` decides whether the
    policy applies to a command (default: always).
    """

    name: str
    approvals: int = 1
    permission: str | None = None
    group: str | None = None
    expires_after: timedelta | None = None
    approve_rule: Rule | None = None
    when: Callable[[Any], bool] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Refuse a policy that cannot be met.

        Raises:
            ImproperlyConfigured: The name is malformed; ``approvals`` is not
                1 to 10; not exactly one of ``permission`` and ``group`` is
                given; ``expires_after`` is not more than zero and at most
                90 days; or ``approve_rule`` is not an access rule.
        """
        _checked_name(self.name, "A policy's name")
        if not 1 <= self.approvals <= MAX_APPROVALS:
            raise ImproperlyConfigured(
                f"Policy {self.name!r} asks for 1 to {MAX_APPROVALS} approvals, "
                f"not {self.approvals!r}."
            )
        if (self.permission is None) == (self.group is None):
            raise ImproperlyConfigured(
                f"Policy {self.name!r} names its approvers by a permission or "
                "by a group: one of the two."
            )
        if self.expires_after is not None and not (
            timedelta(0) < self.expires_after <= LONGEST_WAIT
        ):
            raise ImproperlyConfigured(
                f"Policy {self.name!r} expires after more than nothing and at "
                "most 90 days."
            )
        if self.approve_rule is not None and not _is_rule(self.approve_rule):
            raise ImproperlyConfigured(
                f"Policy {self.name!r}'s approve_rule is not an access rule."
            )

    def applies(self, command: object) -> bool:
        """Whether the policy asks for approval of ``command``."""
        return self.when is None or self.when(command)


def _is_rule(rule: object) -> bool:
    """Whether ``rule`` is a ``BasePermission``, or a subclass of it."""
    return isinstance(rule, BasePermission) or (
        isinstance(rule, type) and issubclass(rule, BasePermission)
    )


class Threshold:
    """Amounts, one per currency, over which a policy applies.

    ``Threshold({"USD": "10000", "EGP": "500000"}).exceeded_by(amounts)``
    sums ``amounts`` per currency and says whether any sum is more than
    its currency's amount. A currency without an amount is never over.
    """

    def __init__(self, amounts: Mapping[str, str | Money]) -> None:
        """Read each amount as ``Money`` of its currency.

        Raises:
            ImproperlyConfigured: A currency is unknown, or an amount is not
                exact in it, or is less than zero.
        """
        limits: dict[str, Money] = {}
        for code, amount in amounts.items():
            try:
                limit = amount if isinstance(amount, Money) else Money.of(amount, code)
            except (MoneyError, ArithmeticError, TypeError, ValueError):
                raise ImproperlyConfigured(
                    f"A threshold is an exact amount of its currency: {code} "
                    f"{amount!r}."
                ) from None
            if limit.currency.code != code or limit.amount < Decimal(0):
                raise ImproperlyConfigured(
                    f"A threshold is an amount of at least zero in its currency: "
                    f"{code} {amount!r}."
                )
            limits[code] = limit
        self.limits: Mapping[str, Money] = limits

    def exceeded_by(self, amounts: Iterable[Money]) -> bool:
        """Whether, in some currency, ``amounts`` add up to more than its limit."""
        totals: dict[str, Decimal] = {}
        for amount in amounts:
            code = amount.currency.code
            totals[code] = totals.get(code, Decimal(0)) + amount.amount
        return any(
            code in self.limits and total > self.limits[code].amount
            for code, total in totals.items()
        )


@dataclass(frozen=True, slots=True, eq=False)
class Guard:
    """A guarded use case: its command, its service, and its policies.

    ``permission`` is what a maker must hold to submit it over HTTP (the
    permission a direct call's route would need); None: any maker who may
    submit requests. ``submit`` makes a direct call submit the command,
    rather than only refuse it.
    """

    action: str
    command: type
    service: Service
    unit_of_work: Callable[[], Any]
    policies: tuple[Policy, ...]
    permission: str | None = None
    submit: bool = False
    adapter: TypeAdapter[Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Check the name and the policies; build the command's adapter."""
        _checked_name(self.action, "A guarded action's name")
        if not self.policies:
            raise ImproperlyConfigured(
                f"Guard {self.action!r} needs at least one policy."
            )
        object.__setattr__(self, "adapter", TypeAdapter(self.command))

    async def __call__(self, command: object, uow: object) -> Any:  # noqa: ANN401 - what the service returns.
        """Run the service on ``command`` in ``uow``."""
        return await self.service(command, uow)

    def policy_for(self, command: object) -> Policy | None:
        """The first policy that applies to ``command``; None: no approval needed."""
        return next((each for each in self.policies if each.applies(command)), None)

    def dump(self, command: object) -> dict[str, Any]:
        """``command`` as JSON-ready arguments."""
        dumped: dict[str, Any] = self.adapter.dump_python(command, mode="json")
        return dumped

    def load(self, arguments: object) -> Any:  # noqa: ANN401 - the guard's command.
        """The command ``arguments`` make, validated (``pydantic.ValidationError``)."""
        return self.adapter.validate_python(arguments)

    def policy_named(self, name: str) -> Policy | None:
        """The guard's policy called ``name``, if it still has one."""
        return next((each for each in self.policies if each.name == name), None)


on_guard: HookPoint[Guard] = HookPoint("approval guard")
"""The guards the installed domains registered: ``on_guard(Guard(...))``."""


def guard(
    action: str,
    service: Service,
    *,
    unit_of_work: Callable[[], Any],
    policies: Sequence[Policy],
    command: type | None = None,
    permission: str | None = None,
    submit: bool = False,
    domain: str | None = None,
) -> Guard:
    """Register the guard of a use case decorated ``requires_approval(action)``.

    Call it in ``DomainConfig.ready()`` (or name the installed ``domain``
    it belongs to, as ``HookPoint`` takes it). ``command`` defaults to the
    type of the service's first argument.

    Raises:
        ImproperlyConfigured: The service is not decorated for ``action``
            (calling it directly would skip approval), its command cannot be
            told, or a policy or the name is malformed.
    """
    if getattr(service, ACTION_MARK, None) != action:
        raise ImproperlyConfigured(
            f"Decorate the service of {action!r} with "
            f"@requires_approval({action!r}): without it, calling the service "
            "directly would skip approval."
        )
    if command is None:
        command = _command_of(action, service)
    registered = Guard(
        action,
        command,
        service,
        unit_of_work,
        tuple(policies),
        permission=permission,
        submit=submit,
    )
    on_guard(registered, domain=domain)
    return registered


def _command_of(action: str, service: Service) -> type:
    """The type of the first argument of ``service``, which must be a class.

    ``requires_approval`` copies the service's annotations to its wrapper.
    """
    hints = get_type_hints(service)
    first = next(iter(hints.items()), None)
    if first is None or first[0] == "return" or not isinstance(first[1], type):
        raise ImproperlyConfigured(
            f"Name the command of {action!r}: guard(..., command=...), or "
            "annotate the first argument of its service."
        )
    return first[1]


def guards() -> dict[str, Guard]:
    """Every registered guard, by action.

    Raises:
        ImproperlyConfigured: Two domains registered the same action.
    """
    found: dict[str, Guard] = {}
    for hook in on_guard.hooks():
        if hook.call.action in found:
            raise ImproperlyConfigured(
                f"Two guards are registered as {hook.call.action!r}."
            )
        found[hook.call.action] = hook.call
    return found


def guard_for(action: str) -> Guard:
    """The guard registered as ``action``.

    Raises:
        UnknownAction: None is.
    """
    found = guards().get(action)
    if found is None:
        raise UnknownAction("No guarded command has that name.")
    return found


@dataclass(frozen=True, slots=True)
class Running:
    """The approved command being run, which its guard lets through."""

    request_id: Any
    action: str
    command: object


_running: ContextVar[Running | None] = ContextVar(
    "domainkit_approvals_running", default=None
)


@contextmanager
def running(request_id: object, action: str, command: object) -> Iterator[Running]:
    """Let ``command`` through ``action``'s guard while the block runs."""
    current = Running(request_id, action, command)
    token = _running.set(current)
    try:
        yield current
    finally:
        _running.reset(token)


def approved(action: str, command: object) -> bool:
    """Whether ``command`` is the approved command of ``action`` being run."""
    current = _running.get()
    return (
        current is not None and current.action == action and current.command == command
    )


async def check(action: str, command: object) -> None:
    """Refuse ``command`` unless it is approved, or needs no approval.

    Raises:
        ImproperlyConfigured: No guard is registered as ``action``.
        ApprovalRequired: It needs approval, and is not the approved
            command being run.
        ApprovalPending: The same, for a guard that submits: it was
            submitted, as the request ``request_id``.
    """
    if approved(action, command):
        return
    registered = guards().get(action)
    if registered is None:
        raise ImproperlyConfigured(
            f"{action!r} requires approval, but no guard is registered for it: "
            "install domainkit.contrib.approvals and call guard() in ready()."
        )
    if registered.policy_for(command) is None:
        return
    if registered.submit:
        from .services import submit_directly

        request = await submit_directly(registered, command)
        raise ApprovalPending(
            "This command needs approval: it was submitted for approval.", request.id
        )
    raise ApprovalRequired("This command needs approval: submit it for approval.")


def requires_approval[F: Service](action: str) -> Callable[[F], F]:
    """Guard a use case: called directly, it needs no approval, or is refused.

    The service is ``service(command, uow)``; register its policies with
    ``guard(action, service, ...)`` in ``ready()``. Without a guard
    registered, every call is refused (``ImproperlyConfigured``).
    """
    _checked_name(action, "A guarded action's name")

    def decorate(service: F) -> F:
        @functools.wraps(service)
        async def guarded(command: object, uow: object) -> Any:  # noqa: ANN401 - what the service returns.
            await check(action, command)
            return await service(command, uow)

        setattr(guarded, ACTION_MARK, action)
        return guarded  # type: ignore[return-value]

    return decorate
