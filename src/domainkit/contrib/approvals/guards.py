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
command it approved. A guard with ``by_maker=True`` leaves that run to
its maker: the same call, made again once the request is approved, runs
it, once.
"""

import copy
import functools
import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final, cast, get_type_hints

from pydantic import TypeAdapter

from domainkit.apps.hooks import HookPoint
from domainkit.ddd.tenancy import no_tenant
from domainkit.ddd.usecases import UseCase
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import BasePermission, Rule
from domainkit.money import Money, MoneyError

from .exceptions import ApprovalPending, ApprovalRequired, PolicyRemoved, UnknownAction
from .settings import LONGEST_WAIT, MAX_APPROVALS

type Service = Callable[[Any, Any], Awaitable[Any]]
"""An application service: ``service(command, uow)``."""
type Parties = Callable[[Any], Iterable[str] | Awaitable[Iterable[str]]]
"""Who a command is about: ``parties(command)``, plain or ``async``."""
type ShownValues = Mapping[str, object] | Iterable[tuple[str, object]]
"""Labelled values: a text, or a list of texts, under each label."""
type Show = Callable[[Any], ShownValues | Awaitable[ShownValues]]
"""What a command's request shows: ``show(command)``, plain or ``async``."""

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

    Approvers are whoever holds ``permission``, the members of ``group``
    (a principal with ``groups``, such as ``contrib.auth``'s users), or
    whoever ``approver_rule`` (an access rule: ``IsSuperuser()``, say)
    allows: one of the three. The maker never counts, even in the group,
    nor does anyone the guard names as a party to the command, and one
    person counts once. ``approve_rule`` is an access rule
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
    approver_rule: Rule | None = None

    def __post_init__(self) -> None:
        """Refuse a policy that cannot be met.

        Raises:
            ImproperlyConfigured: The name is malformed; ``approvals`` is not
                1 to 10; not exactly one of ``permission``, ``group``, and
                ``approver_rule`` is given; ``expires_after`` is not more
                than zero and at most 90 days; or ``approve_rule`` or
                ``approver_rule`` is not an access rule.
        """
        _checked_name(self.name, "A policy's name")
        if not 1 <= self.approvals <= MAX_APPROVALS:
            raise ImproperlyConfigured(
                f"Policy {self.name!r} asks for 1 to {MAX_APPROVALS} approvals, "
                f"not {self.approvals!r}."
            )
        named = (self.permission, self.group, self.approver_rule)
        if sum(each is not None for each in named) != 1:
            raise ImproperlyConfigured(
                f"Policy {self.name!r} names its approvers by a permission, a "
                "group, or an access rule: one of the three."
            )
        if self.expires_after is not None and not (
            timedelta(0) < self.expires_after <= LONGEST_WAIT
        ):
            raise ImproperlyConfigured(
                f"Policy {self.name!r} expires after more than nothing and at "
                "most 90 days."
            )
        for rule in (self.approve_rule, self.approver_rule):
            if rule is not None and not _is_rule(rule):
                raise ImproperlyConfigured(
                    f"Policy {self.name!r} names a rule that is not an access rule."
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
    its currency's amount. A currency without an amount is always over: a
    control fails closed for the currencies it does not name (FIN-89),
    unless ``ANY_CURRENCY`` (``"*"``) gives an amount for all of them,
    read in each one's own units (``{"USD": "10000", "*": "1000"}``).
    """

    def __init__(self, amounts: Mapping[str, str | Money]) -> None:
        """Read each amount as ``Money`` of its currency; ``"*"``'s as a number.

        Raises:
            ImproperlyConfigured: A currency is unknown, or an amount is not
                exact in it, or is less than zero.
        """
        limits: dict[str, Money] = {}
        self.any: Decimal | None = None
        """The amount of every currency not named, if ``"*"`` gives one."""
        for code, amount in amounts.items():
            if code == ANY_CURRENCY:
                self.any = _any_amount(amount)
                continue
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
        return any(self._over(code, total) for code, total in totals.items())

    def _over(self, code: str, total: Decimal) -> bool:
        """Whether ``total`` of ``code`` is more than its amount, or it has none."""
        if code in self.limits:
            return total > self.limits[code].amount
        return self.any is None or total > self.any


ANY_CURRENCY: Final = "*"
"""The key of a threshold's amount for every currency it does not name."""
_ANY_AMOUNT: Final = re.compile(r"\d{1,38}(\.\d{1,18})?")


def _any_amount(amount: object) -> Decimal:
    """``"*"``'s amount: a number of at least zero, as text.

    Raises:
        ImproperlyConfigured: It is not.
    """
    if not (isinstance(amount, str) and _ANY_AMOUNT.fullmatch(amount)):
        raise ImproperlyConfigured(
            f"A threshold for every currency ('*') is a number of at least zero, "
            f"as text: {amount!r}."
        )
    return Decimal(amount)


@dataclass(frozen=True, slots=True, eq=False)
class Guard:
    """A guarded use case: its command, its service, and its policies.

    ``permission`` is what a maker must hold to submit it over HTTP (the
    permission a direct call's route would need); None: any maker who may
    submit requests. ``maker_rule`` is an access rule the maker must pass
    as well (``IsSuperuser()`` to make superusers). ``parties`` names, for
    a command, the people it is about (the user a grant promotes), who may
    not decide it: a function of the command, or a coroutine function that
    may read the database (user report P3). ``submit`` makes a direct call
    submit the command, rather than only refuse it.

    ``maker_fields`` names the command's fields that hold who acts
    (``invited_by``, ``granted_by``): the submit route sets them to the
    maker, whatever the client sent, a command submitted otherwise must
    name its maker there, and the run checks them again. ``http=False``
    keeps the command off the submit route: only the project's own code
    submits it (``submit_command``, or a direct call with ``submit``).

    ``by_maker=True`` leaves the approved command to its maker, not the
    worker: a direct call submits it (``ApprovalPending``), as ``submit``
    does, unless a request of the caller's holding exactly that command
    waits already; once it is approved, the same call by the same maker
    runs the service, as the approved request, and spends it. What the
    command gives goes to its maker (an impersonation's token), and what
    it takes is never stored (a password).

    ``show`` says what a person deciding the command is shown of it, in
    place of its arguments by name (ADR 0173): labelled values, each a
    text or a list of texts, in the request's language, from a function
    of the command, or a coroutine function that may read the database
    (the user a grant is for, by name rather than by id). It is read when
    shown, never stored with the request.

    ``everywhere=True`` says the command acts on rows that belong to no
    tenant (the ledger, users): its makers and approvers are checked as
    where no tenant is served, so a member's role in the tenant served
    never makes one of them (ADR 0183).
    """

    action: str
    command: type
    service: Service
    unit_of_work: Callable[[], Any]
    policies: tuple[Policy, ...]
    permission: str | None = None
    submit: bool = False
    maker_rule: Rule | None = None
    parties: Parties | None = None
    maker_fields: tuple[str, ...] = ()
    http: bool = True
    by_maker: bool = False
    show: Show | None = None
    everywhere: bool = False
    adapter: TypeAdapter[Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Check the name, the service, and the policies; build the command's adapter.

        However the guard is registered (``guard()``, or ``on_guard``), its
        service must be decorated ``@requires_approval`` for its action:
        without it, calling the service directly would skip approval (DSGN-18).
        """
        _checked_name(self.action, "A guarded action's name")
        _check_decorated(self.action, self.service)
        if not self.policies:
            raise ImproperlyConfigured(
                f"Guard {self.action!r} needs at least one policy."
            )
        fields = set(get_type_hints(self.command))
        if unknown := sorted(set(self.maker_fields) - fields):
            raise ImproperlyConfigured(
                f"Guard {self.action!r}: maker_fields names {', '.join(unknown)}, "
                f"which {self.command.__name__} has not."
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

    def with_maker(self, arguments: object, maker: str) -> object:
        """``arguments`` with each of ``maker_fields`` set to ``maker``."""
        if not self.maker_fields or not isinstance(arguments, Mapping):
            return arguments
        return {**arguments, **dict.fromkeys(self.maker_fields, maker)}

    def names_its_maker(self, command: object, maker: str) -> bool:
        """Whether each of ``maker_fields`` of ``command`` holds ``maker``."""
        return all(str(getattr(command, name)) == maker for name in self.maker_fields)

    def parties_of(self, command: object) -> frozenset[str]:
        """The people ``command`` is about, who may not decide it.

        Raises:
            ImproperlyConfigured: ``parties`` is a coroutine function: await
                ``aparties_of`` instead.
        """
        if self.parties is None:
            return frozenset()
        found = self.parties(command)
        if inspect.iscoroutine(found):
            found.close()
            raise ImproperlyConfigured(
                f"Guard {self.action!r} names its parties with a coroutine "
                "function: await aparties_of(command)."
            )
        return frozenset(cast("Iterable[str]", found))  # Not awaitable: a plain one's.

    async def aparties_of(self, command: object) -> frozenset[str]:
        """The people ``command`` is about, ``parties`` awaited if it is async."""
        if self.parties is None:
            return frozenset()
        found = self.parties(command)
        if inspect.isawaitable(found):
            found = await found
        return frozenset(found)

    async def shown(self, command: object) -> list[tuple[str, object]] | None:
        """What ``show`` says of ``command``, awaited if need be; None without one."""
        if self.show is None:
            return None
        found = self.show(command)
        if inspect.isawaitable(found):
            found = await found
        pairs = found.items() if isinstance(found, Mapping) else found
        return [(str(label), value) for label, value in pairs]

    def policy_named(self, name: str) -> Policy | None:
        """The guard's policy called ``name``, if it still has one."""
        return next((each for each in self.policies if each.name == name), None)

    @contextmanager
    def checking(self) -> Iterator[None]:
        """Check its makers and approvers inside the block, as ``everywhere`` says.

        With ``everywhere``, as where no tenant is served: a member's keys
        in the tenant served do not count.
        """
        if not self.everywhere:
            yield
            return
        with no_tenant():
            yield


on_guard: HookPoint[Guard] = HookPoint("approvals.guard")
"""The guards the installed domains registered: ``guard()`` registers here."""


def guard(  # noqa: PLR0913
    action: str,
    service: Service | UseCase[Any],
    *,
    unit_of_work: Callable[[], Any],
    policies: Sequence[Policy],
    command: type | None = None,
    permission: str | None = None,
    submit: bool = False,
    maker_rule: Rule | None = None,
    parties: Parties | None = None,
    maker_fields: Iterable[str] = (),
    http: bool = True,
    by_maker: bool = False,
    show: Show | None = None,
    everywhere: bool = False,
    domain: str | None = None,
) -> Guard:
    """Register the guard of a use case decorated ``requires_approval(action)``.

    Call it in ``DomainConfig.ready()`` (or name the installed ``domain``
    it belongs to, as ``HookPoint`` takes it). ``command`` defaults to the
    type of the service's first argument.

    A ``@use_case`` decorated ``@requires_approval`` is a service too: its
    command is the use case's, and the guard runs its guarded ``run``.

    Raises:
        ImproperlyConfigured: The service is not decorated for ``action``
            (calling it directly would skip approval), its command cannot be
            told, or a policy or the name is malformed.
    """
    _check_decorated(action, service)
    if isinstance(service, UseCase):
        command = command or service.command
        service = service.run
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
        maker_rule=maker_rule,
        parties=parties,
        maker_fields=tuple(maker_fields),
        http=http,
        by_maker=by_maker,
        show=show,
        everywhere=everywhere,
    )
    on_guard(registered, domain=domain)
    return registered


def _check_decorated(action: str, service: Service | UseCase[Any]) -> None:
    """Refuse a service not decorated ``@requires_approval(action)``.

    Raises:
        ImproperlyConfigured: It is not: calling it directly would skip approval.
    """
    if getattr(service, ACTION_MARK, None) != action:
        raise ImproperlyConfigured(
            f"Decorate the service of {action!r} with "
            f"@requires_approval({action!r}): without it, calling the service "
            "directly would skip approval."
        )


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


def policy_of(request: Any) -> tuple[Guard, Policy]:  # noqa: ANN401 - a request.
    """The guard of ``request``'s action, and the policy it waits under.

    Raises:
        PolicyRemoved: The guard, or its policy of that name, no longer exists:
            the request is neither decided nor run, rather than fall back
            to rules it was not submitted under.
    """
    registered = guards().get(request.action)
    policy = None if registered is None else registered.policy_named(request.policy)
    if registered is None or policy is None:
        raise PolicyRemoved(
            "The request's guard or policy no longer exists; it is neither "
            "decided nor run."
        )
    return registered, policy


@dataclass(frozen=True, slots=True)
class Running:
    """The approved command being run, which its guard lets through."""

    request_id: Any
    action: str
    command: object
    maker: str | None = None
    """Who submitted it: the run acts as them."""
    required: int = 0
    """How many approvals its policy asked for: the ledger's threshold lets
    the entries it books through when that is at least ``ENTRY_APPROVALS``
    (FIN-92)."""


_running: ContextVar[Running | None] = ContextVar(
    "domainkit_approvals_running", default=None
)


@contextmanager
def running(
    request_id: object,
    action: str,
    command: object,
    maker: str | None = None,
    required: int = 0,
) -> Iterator[Running]:
    """Let ``command`` through ``action``'s guard while the block runs.

    ``required`` is how many approvals the request had to have.
    """
    current = Running(request_id, action, command, maker, required)
    token = _running.set(current)
    try:
        yield current
    finally:
        _running.reset(token)


def current_run() -> Running | None:
    """The approved command being run here, if any."""
    return _running.get()


_when_spent: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "domainkit_approvals_when_spent", default=None
)


@contextmanager
def checked_when_spent(check: Callable[[], Awaitable[None]]) -> Iterator[None]:
    """Have a ``by_maker`` call in the block await ``check`` as it spends a request.

    Under the approved request's lock, once it is known to be spendable and
    before it is marked executed: only the call that claims the approval
    runs it, and one that raises leaves the request approved. Calls that
    lose the claim, or find no approval, never run it (user report A19).
    """
    token = _when_spent.set(check)
    try:
        yield
    finally:
        _when_spent.reset(token)


def when_spent() -> Callable[[], Awaitable[None]] | None:
    """The check a ``by_maker`` call awaits as it spends a request, if any."""
    return _when_spent.get()


def approved(action: str, command: object) -> bool:
    """Whether ``command`` is the approved command of ``action`` being run."""
    current = _running.get()
    return (
        current is not None and current.action == action and current.command == command
    )


async def check(action: str, command: object) -> Running | None:
    """Refuse ``command`` unless it is approved, or needs no approval.

    For a guard ``by_maker``, an approved request of the caller's holding
    ``command`` is spent here, and returned as the run the call is: the
    service runs as it.

    Raises:
        ImproperlyConfigured: No guard is registered as ``action``.
        ApprovalRequired: It needs approval, and is not the approved
            command being run.
        ApprovalPending: The same, for a guard that submits: it was
            submitted, as the request ``request_id``.
    """
    if approved(action, command):
        return None
    registered = guards().get(action)
    if registered is None:
        raise ImproperlyConfigured(
            f"{action!r} requires approval, but no guard is registered for it: "
            "install domainkit.contrib.approvals and call guard() in ready()."
        )
    if registered.policy_for(command) is None:
        return None
    if registered.by_maker and current_run() is None:
        from .services import run_by_maker

        return await run_by_maker(registered, command)
    if registered.submit:
        from .services import submit_directly

        request = await submit_directly(registered, command)
        raise ApprovalPending(
            "This command needs approval: it was submitted for approval.", request.id
        )
    raise ApprovalRequired("This command needs approval: submit it for approval.")


def requires_approval[F: Service | UseCase[Any]](action: str) -> Callable[[F], F]:
    """Guard a use case: called directly, it needs no approval, or is refused.

    The service is ``service(command, uow)``, or a ``@use_case`` (decorate
    it above ``@use_case``): its call and its ``run``, which routes call,
    are both guarded (user report P1). Register its policies with
    ``guard(action, service, ...)`` in ``ready()``. Without a guard
    registered, every call is refused (``ImproperlyConfigured``).
    """
    _checked_name(action, "A guarded action's name")

    def decorate(service: F) -> F:
        if isinstance(service, UseCase):
            return _guarded_use_case(action, service)  # type: ignore[return-value]

        @functools.wraps(service)
        async def guarded(command: object, uow: object) -> Any:  # noqa: ANN401 - what the service returns.
            return await _checked_run(action, command, service, uow)

        setattr(guarded, ACTION_MARK, action)
        return guarded  # type: ignore[return-value]

    return decorate


async def _checked_run(
    action: str, command: object, service: Service, uow: object
) -> Any:  # noqa: ANN401 - what the service returns.
    """``service(command, uow)``, once ``check`` lets it through.

    A run its maker's call is (a guard ``by_maker``) runs inside it.
    """
    run = await check(action, command)
    if run is None:
        return await service(command, uow)
    with running(run.request_id, run.action, run.command, run.maker, run.required):
        return await service(command, uow)


def _guarded_use_case(action: str, use_case: UseCase[Any]) -> UseCase[Any]:
    """A copy of ``use_case`` whose ``run`` checks approval first.

    ``UseCase.__call__`` runs ``run``, and routes wire ``run``: guarding it
    guards every way in. The original stays as it was.
    """
    unguarded = use_case.run

    async def run(command: Any, uow: Any) -> Any:  # noqa: ANN401 - the use case's.
        return await _checked_run(action, command, unguarded, uow)

    guarded = copy.copy(use_case)
    guarded.run = run  # type: ignore[method-assign]
    for each in (run, guarded):
        setattr(each, ACTION_MARK, action)
    return guarded
