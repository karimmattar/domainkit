"""Guards and policies: declared once, checked when declared, failing closed.

No database: a guard is registered and checked against what it says, and
a guarded service refuses a command that needs approval before it runs.
"""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

import pytest

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.approvals import (
    ApprovalRequired,
    Guard,
    Policy,
    Threshold,
    UnknownAction,
    approved,
    guard,
    guards,
    on_guard,
    requires_approval,
)
from domainkit.contrib.approvals.guards import check, guard_for, running
from domainkit.ddd import Command
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import IsAuthenticated
from domainkit.money import Money

pytestmark = pytest.mark.anyio

ALPHA: Final = "domainkit_tests.sample_domains.alpha"
DOMAINS: Final = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.audit",
    "domainkit.contrib.approvals",
    ALPHA,
]


class Refund(Command):
    """A refund: the command of a guarded use case."""

    amount: Money


ran: list[object] = []


@requires_approval("tests.refund")
async def refund(command: Refund, uow: object) -> str:
    """Record the run; the use case's work."""
    ran.append(command)
    return "refunded"


def large(command: Refund) -> bool:
    """Refunds of more than 100 dollars."""
    return Threshold({"USD": "100"}).exceeded_by([command.amount])


POLICY: Final = Policy(
    "tests.refunds", approvals=2, permission="tests.approve", when=large
)


@pytest.fixture
def installed() -> Iterator[None]:
    """Messaging, audit, approvals, and a sample domain, set up without a database."""
    registry.clear()
    ran.clear()
    with override_settings(INSTALLED_DOMAINS=DOMAINS):
        setup()
        yield
    registry.clear()


def register(**options: Any) -> Guard:  # noqa: ANN401 - guard()'s options.
    """Guard ``refund`` for the sample domain."""
    return guard(
        "tests.refund",
        refund,
        unit_of_work=object,
        policies=[POLICY],
        domain="alpha",
        **options,
    )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"name": "Refunds"}, "dotted lower-case words"),
        ({"name": "tests." + "x" * 128}, "at most 128 characters"),
        ({"approvals": 0}, "1 to 10 approvals"),
        ({"approvals": 11}, "1 to 10 approvals"),
        ({"permission": None}, "one of the two"),
        ({"group": "treasurers"}, "one of the two"),
        ({"expires_after": timedelta(0)}, "at most 90 days"),
        ({"expires_after": timedelta(days=91)}, "at most 90 days"),
        ({"approve_rule": "step-up"}, "not an access rule"),
    ],
)
def test_a_policy_that_cannot_be_met_is_refused(
    options: dict[str, Any], message: str
) -> None:
    """Bounds, one kind of approver, and a real rule: checked when declared."""
    given: dict[str, Any] = {"name": "tests.p", "permission": "tests.approve"}
    with pytest.raises(ImproperlyConfigured, match=message):
        Policy(**(given | options))


def test_a_policy_takes_rules_and_their_classes() -> None:
    """``approve_rule`` is a rule, or a rule class; ``when`` defaults to always."""
    policy = Policy(
        "tests.p",
        group="treasurers",
        expires_after=timedelta(days=90),
        approve_rule=IsAuthenticated,
    )
    assert policy.applies(object())
    assert Policy("tests.p", permission="a.b", approve_rule=IsAuthenticated()).name


def test_thresholds_sum_per_currency_and_never_mix_them() -> None:
    """Over means more than the limit, in that currency; others are never over."""
    threshold = Threshold({"USD": "100", "EGP": Money.of("5000", "EGP")})
    usd = Money.of("50", "USD")

    assert not threshold.exceeded_by([usd, usd])
    assert threshold.exceeded_by([usd, usd, Money.of("0.01", "USD")])
    assert not threshold.exceeded_by([Money.of("1000000", "EUR")])
    assert not threshold.exceeded_by([Money.of("5000", "EGP")])
    assert threshold.exceeded_by([Money.of("5000.01", "EGP")])
    assert threshold.limits["USD"].amount == Decimal("100.00")


@pytest.mark.parametrize(
    "amounts",
    [
        {"XXQ": "1"},
        {"USD": "1.001"},
        {"USD": "-1"},
        {"USD": 1.5},
        {"USD": Money.of("1", "EUR")},
    ],
)
def test_a_threshold_is_an_exact_amount_of_its_currency(
    amounts: dict[str, Any],
) -> None:
    """Unknown currencies, places it lacks, floats, and another currency's money."""
    with pytest.raises(ImproperlyConfigured, match="threshold"):
        Threshold(amounts)


def test_guards_are_registered_by_name_with_their_command(installed: None) -> None:
    """``guard()`` reads the command from the service; ``guards()`` finds it."""
    registered = register(permission="tests.add_refund")

    assert guard_for("tests.refund") is registered
    assert registered.command is Refund
    assert registered.permission == "tests.add_refund"
    assert registered.policy_named("tests.refunds") is POLICY
    assert registered.policy_named("tests.other") is None
    assert guards() == {"tests.refund": registered}
    with pytest.raises(UnknownAction):
        guard_for("tests.other")


def test_an_undecorated_or_unreadable_service_is_refused(installed: None) -> None:
    """A guard needs the decorated service, and a command it can tell."""

    async def plain(command: Refund, uow: object) -> None:
        """Not decorated."""

    with pytest.raises(ImproperlyConfigured, match="@requires_approval"):
        guard("tests.refund", plain, unit_of_work=object, policies=[POLICY])

    @requires_approval("tests.untyped")
    async def untyped(command, uow) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
        """Only the answer is hinted: no command."""

    with pytest.raises(
        ImproperlyConfigured,
        match=r"^Name the command of 'tests\.untyped': guard\(\.\.\., "
        r"command=\.\.\.\), or annotate the first argument of its service\.$",
    ):
        guard("tests.untyped", untyped, unit_of_work=object, policies=[POLICY])

    @requires_approval("tests.bare")
    async def bare(command, uow):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        """No hints at all."""

    with pytest.raises(ImproperlyConfigured, match="Name the command"):
        guard("tests.bare", bare, unit_of_work=object, policies=[POLICY])

    @requires_approval("tests.generic")
    async def generic(command: list[int], uow: object) -> None:
        """A hint that is no class."""

    with pytest.raises(ImproperlyConfigured, match="Name the command"):
        guard("tests.generic", generic, unit_of_work=object, policies=[POLICY])
    named = guard(
        "tests.generic",
        generic,
        unit_of_work=object,
        policies=[POLICY],
        command=Refund,
        domain="alpha",
    )
    assert named.command is Refund


def test_two_guards_of_one_name_and_none_without_policies_are_refused(
    installed: None,
) -> None:
    """A name is one use case's; a guard without a policy guards nothing."""
    register()
    on_guard(Guard("tests.refund", Refund, refund, object, (POLICY,)), domain="alpha")
    with pytest.raises(ImproperlyConfigured, match="Two guards"):
        guards()
    with pytest.raises(ImproperlyConfigured, match="at least one policy"):
        Guard("tests.empty", Refund, refund, object, ())
    with pytest.raises(
        ImproperlyConfigured,
        match=r"^A guarded action's name is dotted lower-case words "
        r"\('payments\.refund'\), at most 128 characters: 'Refund'\.$",
    ):
        requires_approval("Refund")


async def test_a_guarded_service_refuses_what_needs_approval(installed: None) -> None:
    """Small refunds run; large ones are refused, and never reach the service."""
    register()
    small = Refund(amount=Money.of("100", "USD"))
    big = Refund(amount=Money.of("100.01", "USD"))

    assert await refund(small, None) == "refunded"
    with pytest.raises(ApprovalRequired):
        await refund(big, None)
    assert ran == [small]


async def test_only_the_approved_command_passes_while_it_runs(installed: None) -> None:
    """The run of an approved request lets its own command through, no other."""
    register()
    big = Refund(amount=Money.of("500", "USD"))
    other = Refund(amount=Money.of("600", "USD"))

    with running("r-1", "tests.refund", big):
        assert approved("tests.refund", big)
        assert not approved("tests.other", big)
        assert await refund(big, None) == "refunded"
        with pytest.raises(ApprovalRequired):
            await refund(other, None)
    assert not approved("tests.refund", big)
    with pytest.raises(ApprovalRequired):
        await refund(big, None)


async def test_a_guard_never_registered_fails_closed(installed: None) -> None:
    """A decorated service without its guard refuses every call."""
    with pytest.raises(ImproperlyConfigured, match="no guard is registered"):
        await check("tests.refund", Refund(amount=Money.of("1", "USD")))
    assert ran == []


async def test_a_guard_that_submits_needs_someone_acting(installed: None) -> None:
    """Without an actor there is no maker to submit for: refused."""
    register(submit=True)
    big = Refund(amount=Money.of("500", "USD"))
    with pytest.raises(ApprovalRequired) as raised:
        await refund(big, None)
    assert raised.value.code == "approval_required"
    with (
        bind_request(
            RequestContext(request_id="r", client_ip=None, method="POST", path="/")
        ),
        pytest.raises(ApprovalRequired),
    ):
        await refund(big, None)
    assert ran == []


def test_nothing_is_guarded_where_its_app_is_missing() -> None:
    """Thresholds and superusers asked for, without the ledger or auth: no guard."""
    registry.clear()
    try:
        with override_settings(
            INSTALLED_DOMAINS=DOMAINS,
            APPROVALS={"ENTRY_THRESHOLDS": {"USD": "1"}, "SUPERUSER_APPROVALS": 1},
        ):
            setup()
            assert guards() == {}
    finally:
        registry.clear()


def test_paths_around_a_service_are_refused_only_where_guarded(installed: None) -> None:
    """``refuse_if_guarded`` refuses a guarded action, and lets any other be."""
    from domainkit.contrib.approvals import refuse_if_guarded

    register()
    refuse_if_guarded("tests.other")
    with pytest.raises(
        ApprovalRequired,
        match=r"^This command needs approval: submit it for approval\.$",
    ):
        refuse_if_guarded("tests.refund")


async def test_approving_asks_no_more_than_the_approvers_without_a_rule(
    installed: None,
) -> None:
    """With no rule of the policy's and none configured, ``MayApprove`` allows."""
    from domainkit.contrib.approvals.models import ApprovalRequest
    from domainkit.contrib.approvals.rules import MayApprove

    register()
    request = ApprovalRequest.submit(
        "tests.refund",
        {},
        maker="m",
        policy="tests.refunds",
        required=2,
        permission="tests.approve",
        group=None,
        expires_at=clock.now() + timedelta(days=1),
    )
    assert await MayApprove().has_object_permission(None, None, request)  # type: ignore[arg-type]
    request.cancel("m", "")
    request.expire()  # Decided already: it stays cancelled.
    assert request.status == "cancelled"


def test_a_requests_life_records_its_events() -> None:
    """Each change records its event, about this request, and dates itself."""
    from domainkit.contrib.approvals import (
        RequestCancelled,
        RequestExpired,
        RequestFailed,
        RequestRejected,
    )
    from domainkit.contrib.approvals.models import ApprovalRequest

    def made() -> ApprovalRequest:
        request = ApprovalRequest.submit(
            "tests.refund",
            {"amount": 1},
            maker="m",
            policy="tests.refunds",
            required=1,
            permission="tests.approve",
            group=None,
            expires_at=clock.now() + timedelta(days=1),
        )
        request.pull_events()
        return request

    def facts(events: list[Any]) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                type(event).__name__,
                {
                    name: value
                    for name, value in vars(event).items()
                    if name not in ("event_id", "occurred_at")
                },
            )
            for event in events
        ]

    def expected(*events: Any) -> list[tuple[str, dict[str, Any]]]:  # noqa: ANN401
        return facts(list(events))

    with clock.freeze_time(clock.now()) as frozen:
        rejected = made()
        rejected.reject("ann", "No.")
        assert facts(rejected.pull_events()) == expected(
            RequestRejected(request_id=rejected.id)
        )
        assert rejected.decided_at == frozen.now()
        cancelled = made()
        cancelled.cancel("m", "Typo.")
        assert facts(cancelled.pull_events()) == expected(
            RequestCancelled(request_id=cancelled.id)
        )
        assert (cancelled.decided_at, cancelled.reason) == (frozen.now(), "Typo.")
        expired = made()
        expired.expire()
        assert facts(expired.pull_events()) == expected(
            RequestExpired(request_id=expired.id)
        )
        assert expired.decided_at == frozen.now()
        failed = made()
        failed.failed("insufficient_funds")
        assert facts(failed.pull_events()) == expected(
            RequestFailed(
                request_id=failed.id, action="tests.refund", error="insufficient_funds"
            )
        )
        assert (failed.executed_at, failed.reason) == (
            frozen.now(),
            "insufficient_funds",
        )


def test_the_routes_rules_by_action() -> None:
    """View and add by model permission; cancel by add; deciding by the policy."""
    from domainkit.contrib.approvals.models import ApprovalRequest
    from domainkit.contrib.approvals.rules import IsApprover, MayApprove, RequestActions

    def described(action: str) -> list[tuple[str, Any]]:
        return [
            (type(rule).__name__, getattr(rule, "action", None))
            for rule in RequestActions().rules_for(action)
        ]

    assert described("approve") == [("IsApprover", None), ("MayApprove", None)]
    assert described("reject") == [("IsApprover", None)]
    assert described("cancel") == [("HasModelPermission", "add")]
    assert described("view") == [("HasModelPermission", "view")]
    assert described("add") == [("HasModelPermission", "add")]
    for action in ("cancel", "view"):
        (rule,) = RequestActions().rules_for(action)
        assert getattr(rule, "model", None) is ApprovalRequest
    assert IsApprover.bounds_scopes
    assert not MayApprove.bounds_scopes


class Someone:
    """A principal of a project's own, with groups as plain names or not at all."""

    id = "p"

    def __init__(self, *, active: bool = True, groups: Any = None) -> None:  # noqa: ANN401
        """Active or not; ``groups`` set as an attribute only when given."""
        self.is_authenticated = True
        self.is_active = active
        if groups is not None:
            self.groups = groups

    async def ahas_perms(self, perms: object, obj: object = None) -> bool:
        """No permissions: only groups."""
        return False


def someone(*, active: bool = True, groups: Any = None) -> Any:  # noqa: ANN401
    """A ``Someone``, typed as any principal."""
    return Someone(active=active, groups=groups)


class Seen:
    """A rule that records what it was asked with, and allows."""

    def __init__(self) -> None:
        """Nothing asked yet."""
        self.asked: list[tuple[str, object, object]] = []

    async def has_permission(self, request: object, user: object) -> bool:
        """Record, allow."""
        self.asked.append(("request", request, user))
        return True

    async def has_object_permission(
        self, request: object, user: object, obj: object
    ) -> bool:
        """Record, allow."""
        self.asked.append(("object", request, user))
        return True


async def test_approvers_by_group_and_the_rule_asked_of_them(installed: None) -> None:
    """Groups as names; an inactive or group-less principal is no approver.

    ``MayApprove`` asks the policy's rule with the request and the
    principal; a request whose guard or policy is gone gets the configured
    rule (none here).
    """
    from domainkit.contrib.approvals.models import ApprovalRequest
    from domainkit.contrib.approvals.rules import IsApprover, MayApprove
    from domainkit.http.access import BasePermission

    seen = Seen()

    class Recorded(BasePermission):
        async def has_permission(self, request: Any, user: Any) -> bool:  # noqa: ANN401
            return await seen.has_permission(request, user)

        async def has_object_permission(
            self,
            request: Any,  # noqa: ANN401
            user: Any,  # noqa: ANN401
            obj: object,
        ) -> bool:
            return await seen.has_object_permission(request, user, obj)

    guard(
        "tests.refund",
        refund,
        unit_of_work=object,
        policies=[Policy("tests.refunds", group="treasurers", approve_rule=Recorded())],
        domain="alpha",
    )

    def request(action: str = "tests.refund", policy: str = "tests.refunds") -> Any:  # noqa: ANN401
        return ApprovalRequest.submit(
            action,
            {},
            maker="m",
            policy=policy,
            required=1,
            permission=None,
            group="treasurers",
            expires_at=clock.now() + timedelta(days=1),
        )

    class Connection:
        scope: dict[str, object] = {}  # noqa: RUF012

    connection: Any = Connection()
    member = someone(groups=["treasurers"])
    rule = IsApprover()
    assert await rule.has_permission(connection, member)
    assert not await rule.has_permission(connection, someone(active=False))
    assert await rule.has_object_permission(connection, member, request())
    assert not await rule.has_object_permission(
        connection, someone(active=False, groups=["treasurers"]), request()
    )
    assert not await rule.has_object_permission(connection, someone(), request())

    assert await MayApprove().has_object_permission(connection, member, request())
    assert seen.asked == [
        ("request", connection, member),
        ("object", connection, member),
    ]
    assert await MayApprove().has_object_permission(
        connection, member, request(action="tests.gone")
    )
    assert await MayApprove().has_object_permission(
        connection, member, request(policy="tests.gone")
    )
    assert len(seen.asked) == 2


def test_the_guards_approvals_brings_follow_their_settings() -> None:
    """Postings and superusers, each policy as ``APPROVALS`` says."""
    registry.clear()
    try:
        with override_settings(
            INSTALLED_DOMAINS=[
                "domainkit.contrib.contenttypes",
                "domainkit.contrib.auth",
                "domainkit.contrib.accounting",
                *DOMAINS,
            ],
            AUTH={
                "PASSWORD_HASHERS": [
                    "domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"
                ]
            },
            APPROVALS={
                "ENTRY_THRESHOLDS": {"USD": "10"},
                "ENTRY_APPROVALS": 3,
                "ENTRY_APPROVERS": "accounting.approve_large",
                "SUPERUSER_APPROVALS": 4,
                "SUPERUSER_APPROVERS": "auth.approve_superuser",
            },
        ):
            setup()
            found = guards()
            entries, superusers = (
                found["accounting.post_entry"],
                found["auth.grant_superuser"],
            )
            assert entries.permission == "accounting.add_entry"
            assert [
                (each.name, each.approvals, each.permission, each.group)
                for each in entries.policies
            ] == [("accounting.large_entry", 3, "accounting.approve_large", None)]
            assert superusers.permission == "auth.approve_superuser"
            assert [
                (each.name, each.approvals, each.permission, each.group)
                for each in superusers.policies
            ] == [("auth.superuser", 4, "auth.approve_superuser", None)]
    finally:
        registry.clear()


async def test_a_missing_guard_is_named_when_refused(installed: None) -> None:
    """The refusal of a decorated service with no guard says what to do."""
    with pytest.raises(
        ImproperlyConfigured,
        match=r"^'tests\.refund' requires approval, but no guard is registered for "
        r"it: install domainkit\.contrib\.approvals and call guard\(\) in ready\(\)\.$",
    ):
        await check("tests.refund", Refund(amount=Money.of("1", "USD")))
