"""Maker-checker's proofs: approvals counted once, run once, and only as approved.

Each runs on SQLite, and on PostgreSQL when ``DOMAINKIT_TEST_POSTGRES_URL``
is set: there twenty approvals of one request really run at once. Every
test starts from a project migrated as ``makemigrations`` and ``migrate``
leave it, with the ledger, messaging, the audit trail, and approvals.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Final

import pytest
from sqlalchemy import func, select, update

from domainkit import clock
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.accounting import (
    Account,
    AccountingUnitOfWork,
    AccountType,
    Line,
    OpenAccount,
    OpenPeriod,
    PostEntry,
    open_account,
    open_period,
    post_entry,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.approvals import (
    ENTRIES,
    AlreadyDecided,
    ApprovalNotNeeded,
    ApprovalPending,
    ApprovalRequest,
    ApprovalRequired,
    ApprovalsUnitOfWork,
    ApproveRequest,
    CancelRequest,
    FingerprintMismatch,
    InvalidArguments,
    MakerCannotDecide,
    NotTheMaker,
    Policy,
    RejectRequest,
    RequestNotFound,
    RequestNotPending,
    RequestStatus,
    SubmitRequest,
    UnknownAction,
    approve_request,
    cancel_request,
    execute_request,
    expire_requests,
    guard,
    reject_request,
    requires_approval,
    submit_command,
    submit_request,
)
from domainkit.contrib.approvals.tables import decisions, requests
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.ddd import Command, DomainError
from domainkit.i18n import gettext, override
from domainkit.money import Money
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    Migrated,
    Shape,
)

pytestmark = pytest.mark.anyio

MESSAGING: Final = "domainkit.contrib.messaging"
AUDIT: Final = "domainkit.contrib.audit"
ACCOUNTING: Final = "domainkit.contrib.accounting"
APPROVALS: Final = "domainkit.contrib.approvals"
FAST: Final = {
    "RETRY_BASE_SECONDS": 0.001,
    "RETRY_MAX_SECONDS": 0.001,
    "TASK_TIMEOUT": 30,
    "VISIBILITY_TIMEOUT": 60,
}
PROJECT: Final = Shape(
    "approvals",
    "approvals",
    {
        "INSTALLED_DOMAINS": [
            CONTENTTYPES,
            AUTH,
            MESSAGING,
            AUDIT,
            ACCOUNTING,
            APPROVALS,
            ALPHA,
        ],
        "MESSAGING": FAST,
        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER]},
        "APPROVALS": {"ENTRY_THRESHOLDS": {"USD": "1000.00"}},
    },
)
"""A project with the ledger, messaging, audit, and approvals, migrated once per run."""
TODAY: Final = date(2026, 10, 30)
APPROVERS: Final = 20


@contextmanager
def refused(
    error: type[DomainError], match: str | None = None
) -> Iterator[pytest.ExceptionInfo[DomainError]]:
    """``pytest.raises(error, match=match)``, its message one the catalogs translate."""
    with pytest.raises(error, match=match) as raised:
        yield raised
    message = str(raised.value)
    with override("ar"):
        assert gettext(message) != message, message


@pytest.fixture
async def project(migrated: Migrated) -> AsyncIterator[str]:
    """The project in a migrated database of the test's own; its URL."""
    with migrated(PROJECT) as url:
        yield url
        await connections.close_all()


def usd(amount: str) -> Money:
    """``amount`` dollars."""
    return Money.of(amount, "USD")


async def books() -> tuple[Account, Account]:
    """A period, and a cash and a sales account."""
    await open_period(
        OpenPeriod(name="2026-10", starts_on=date(2026, 10, 1), ends_on=TODAY),
        AccountingUnitOfWork(),
    )
    cash = await open_account(
        OpenAccount(code="1000", name="Cash", type=AccountType.ASSET, currency="USD"),
        AccountingUnitOfWork(),
    )
    sales = await open_account(
        OpenAccount(code="4000", name="Sales", type=AccountType.INCOME, currency="USD"),
        AccountingUnitOfWork(),
    )
    return cash, sales


def sale(cash: Account, sales: Account, amount: str, reference: str) -> PostEntry:
    """A cash sale of ``amount`` dollars."""
    return PostEntry(
        source="shop",
        reference=reference,
        booked_on=TODAY,
        lines=(Line.debit(cash.id, usd(amount)), Line.credit(sales.id, usd(amount))),
    )


async def count(table: Any, *where: Any) -> int:  # noqa: ANN401 - a table.
    """Rows of ``table`` that match ``where``."""
    async with connections.engine().connect() as connection:
        found = await connection.scalar(
            select(func.count()).select_from(table).where(*where)
        )
    return int(found or 0)


async def stored(request_id: uuid.UUID) -> ApprovalRequest:
    """The request as committed."""
    async with ApprovalsUnitOfWork() as uow:
        return await uow.requests.get(request_id)  # type: ignore[arg-type]


async def approve(request: ApprovalRequest, approver: str) -> ApprovalRequest:
    """``approver`` approves ``request`` as shown."""
    return await approve_request(
        ApproveRequest(
            request_id=request.id, approver=approver, fingerprint=request.fingerprint
        ),
        ApprovalsUnitOfWork(),
    )


async def audited(prefix: str) -> list[tuple[str, str]]:
    """``(action, actor)`` of the audit rows whose action starts with ``prefix``."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            select(audit_log.c.action, audit_log.c.actor)
            .where(audit_log.c.action.startswith(prefix))
            .order_by(audit_log.c.occurred_at, audit_log.c.id)
        )
        return [(row.action, row.actor) for row in rows]


@contextmanager
def acting(actor: str | None) -> Iterator[None]:
    """Run the block as ``actor``, as a request authenticated as them would."""
    with bind_request(
        RequestContext(
            request_id="r", client_ip=None, method="POST", path="/", actor=actor
        )
    ):
        yield


async def test_a_large_entry_runs_once_two_others_approve_it(project: str) -> None:
    """Over its threshold, a posting waits for two approvers, then posts once.

    Posted directly, it is refused, and nothing is booked. Submitted, it
    is approved by two people other than its maker; the second approval
    queues it, and the worker posts it once, however often it runs.
    Every step is in the audit trail, with its actor.
    """
    cash, sales = await books()
    small = await post_entry(sale(cash, sales, "10.00", "s-1"), AccountingUnitOfWork())
    assert small.source == "shop"
    large = sale(cash, sales, "5000.00", "s-2")
    with refused(ApprovalRequired):
        await post_entry(large, AccountingUnitOfWork())
    assert await count(entries) == 1

    with acting("maker"):
        request = await submit_command(ENTRIES, large, maker="maker")
    assert request.status is RequestStatus.PENDING
    assert (request.policy, request.required) == ("accounting.large_entry", 2)
    with acting("ann"):
        first = await approve(request, "ann")
    assert first.status is RequestStatus.PENDING
    await drain()
    assert await count(entries) == 1
    with acting("bob"):
        second = await approve(request, "bob")
    assert second.status is RequestStatus.APPROVED

    await drain()
    await execute_request(request.id)  # Delivered again: nothing more.

    done = await stored(request.id)
    assert done.status is RequestStatus.EXECUTED
    assert done.executed_at is not None
    assert await count(entries, entries.c.reference == "s-2") == 1
    assert await audited("approvals.") == [
        ("approvals.request_submitted", "maker"),
        ("approvals.approval_given", "ann"),
        ("approvals.approval_given", "bob"),
        ("approvals.request_approved", "bob"),
        ("approvals.request_executed", "bob"),
    ]


class OpenVault(Command):
    """Open a vault account: a project's own guarded use case."""

    code: str


@requires_approval("tests.open_vault")
async def open_vault(command: OpenVault, uow: AccountingUnitOfWork) -> Account:
    """Open the vault's account, once approved."""
    return await open_account(
        OpenAccount(
            code=command.code, name="Vault", type=AccountType.ASSET, currency="USD"
        ),
        uow,
    )


def guard_vaults(approvals: int = 2, **options: Any) -> None:  # noqa: ANN401 - guard()'s.
    """Guard ``open_vault`` for the test, as a domain would in ``ready()``."""
    guard(
        "tests.open_vault",
        open_vault,
        unit_of_work=AccountingUnitOfWork,
        policies=[
            Policy(
                "tests.vaults", approvals=approvals, permission="accounting.add_account"
            )
        ],
        domain="alpha",
        **options,
    )


async def test_twenty_approvals_at_once_count_to_ten_and_run_once(
    project: str,
) -> None:
    """Twenty approvers of a request that needs ten, all at once.

    Each approval takes the request's lock, so each sees those before it:
    exactly ten are counted, the tenth approves it, the other ten are
    refused, and the command is queued once and runs once.
    """
    guard_vaults(approvals=10)
    pooled: dict[str, object] = {"POOL_SIZE": 25, "MAX_OVERFLOW": 30}
    if project.startswith("sqlite"):  # One writer at a time: wait as long as it takes.
        pooled = {"CONNECT_ARGS": {"timeout": 60}}
    with override_settings(DATABASES={"default": {"URL": project, **pooled}}):
        await connections.close_all()
        request = await submit_command(
            "tests.open_vault", OpenVault(code="vault"), maker="maker"
        )
        start = asyncio.Event()

        async def one(number: int) -> ApprovalRequest:
            await start.wait()
            return await approve(request, f"approver-{number}")

        running = [asyncio.create_task(one(number)) for number in range(APPROVERS)]
        await asyncio.sleep(0)
        start.set()
        results = await asyncio.wait_for(
            asyncio.gather(*running, return_exceptions=True), 60
        )

        statuses = [
            each.status if isinstance(each, ApprovalRequest) else type(each)
            for each in results
        ]
        assert statuses.count(RequestStatus.APPROVED) == 1
        assert statuses.count(RequestStatus.PENDING) == 9
        assert statuses.count(RequestNotPending) == 10
        assert await count(decisions) == 10
        await drain()
        assert (await stored(request.id)).status is RequestStatus.EXECUTED
        async with AccountingUnitOfWork() as uow:
            assert len(await uow.accounts.filter()) == 1
        await connections.close_all()


async def test_the_maker_never_decides_and_no_one_decides_twice(project: str) -> None:
    """Maker ≠ checker, even where the maker could approve; one person, once."""
    cash, sales = await books()
    request = await submit_command(
        ENTRIES, sale(cash, sales, "5000.00", "s-4"), maker="maker"
    )
    with refused(MakerCannotDecide):
        await approve(request, "maker")
    with refused(MakerCannotDecide):
        await reject_request(
            RejectRequest(request_id=request.id, approver="maker", reason="no"),
            ApprovalsUnitOfWork(),
        )
    await approve(request, "ann")
    with refused(AlreadyDecided):
        await approve(request, "ann")
    with refused(AlreadyDecided):
        await reject_request(
            RejectRequest(request_id=request.id, approver="ann", reason="no"),
            ApprovalsUnitOfWork(),
        )
    with refused(FingerprintMismatch):
        await approve_request(
            ApproveRequest(request_id=request.id, approver="bob", fingerprint="0" * 64),
            ApprovalsUnitOfWork(),
        )
    assert (await stored(request.id)).approvals == 1


async def test_rejected_cancelled_and_expired_requests_never_run(project: str) -> None:
    """One rejection ends a request, with its reason; the maker may withdraw it."""
    cash, sales = await books()
    rejected = await submit_command(
        ENTRIES, sale(cash, sales, "5000.00", "s-5"), maker="maker"
    )
    done = await reject_request(
        RejectRequest(request_id=rejected.id, approver="ann", reason="Not ours."),
        ApprovalsUnitOfWork(),
    )
    assert (done.status, done.reason) == (RequestStatus.REJECTED, "Not ours.")
    with refused(RequestNotPending):
        await approve(rejected, "bob")

    withdrawn = await submit_command(
        ENTRIES, sale(cash, sales, "5000.00", "s-6"), maker="maker"
    )
    with refused(NotTheMaker):
        await cancel_request(
            CancelRequest(request_id=withdrawn.id, maker="ann"), ApprovalsUnitOfWork()
        )
    cancelled = await cancel_request(
        CancelRequest(request_id=withdrawn.id, maker="maker", reason="Typo."),
        ApprovalsUnitOfWork(),
    )
    assert cancelled.status is RequestStatus.CANCELLED
    with refused(RequestNotPending):
        await cancel_request(
            CancelRequest(request_id=withdrawn.id, maker="maker"),
            ApprovalsUnitOfWork(),
        )

    late = await submit_command(
        ENTRIES, sale(cash, sales, "5000.00", "s-7"), maker="maker"
    )
    with clock.freeze_time(clock.now() + timedelta(days=3)):
        with refused(RequestNotPending, "expired"):
            await approve(late, "ann")
        assert await expire_requests() == 1
        assert await expire_requests() == 0
    assert (await stored(late.id)).status is RequestStatus.EXPIRED
    await drain()
    assert await count(entries) == 0
    assert [action for action, _ in await audited("approvals.request_")] == [
        "approvals.request_submitted",
        "approvals.request_rejected",
        "approvals.request_submitted",
        "approvals.request_cancelled",
        "approvals.request_submitted",
        "approvals.request_expired",
    ]


async def test_what_runs_is_what_was_approved(project: str) -> None:
    """A request changed around the application after approval fails; nothing runs.

    The arguments, the fingerprint, or the approvals needed changed in the
    database: the seals no longer hold, so the worker fails the request
    instead of posting what no one approved.
    """
    cash, sales = await books()
    request = await submit_command(
        ENTRIES, sale(cash, sales, "5000.00", "s-8"), maker="maker"
    )
    await approve(request, "ann")
    await approve(request, "bob")
    tampered = dict(request.arguments)
    tampered["lines"] = [
        {**line, "amount": {"amount": "9000.00", "currency": "USD"}}
        for line in request.arguments["lines"]
    ]
    from domainkit.contrib.approvals import fingerprint

    async with connections.engine().begin() as connection:
        await connection.execute(
            update(requests)
            .where(requests.c.id == request.id)
            .values(arguments=tampered, fingerprint=fingerprint(ENTRIES, tampered))
        )
    await drain()
    failed = await stored(request.id)
    assert (failed.status, failed.reason) == (
        RequestStatus.FAILED,
        "fingerprint_mismatch",
    )
    assert await count(entries) == 0


async def test_submissions_are_checked_before_they_are_stored(project: str) -> None:
    """Unknown actions, bad arguments, small entries, and malformed makers."""
    cash, sales = await books()
    small = sale(cash, sales, "10.00", "s-9")
    with refused(UnknownAction):
        await submit_command("tests.nothing", small, maker="maker")
    with refused(InvalidArguments):
        await submit_command(
            ENTRIES, OpenPeriod(name="x", starts_on=TODAY, ends_on=TODAY), maker="maker"
        )
    with refused(ApprovalNotNeeded):
        await submit_command(ENTRIES, small, maker="maker")
    with refused(InvalidArguments):
        await submit_command(ENTRIES, sale(cash, sales, "5000.00", "x"), maker="")
    with refused(InvalidArguments):
        await submit_request(
            SubmitRequest(action=ENTRIES, arguments={"source": 1}, maker="maker"),
            ApprovalsUnitOfWork(),
        )
    made = await submit_request(
        SubmitRequest(
            action=ENTRIES,
            arguments={
                "source": "shop",
                "reference": "s-10",
                "booked_on": "2026-10-30",
                "lines": [
                    {
                        "account_id": str(cash.id),
                        "side": "debit",
                        "amount": {"amount": "5000", "currency": "USD"},
                    },
                    {
                        "account_id": str(sales.id),
                        "side": "credit",
                        "amount": {"amount": "5000", "currency": "USD"},
                    },
                ],
            },
            maker="maker",
        ),
        ApprovalsUnitOfWork(),
    )
    # What is stored is the command read back: amounts at their places, memos.
    assert made.arguments["lines"][0]["amount"] == {
        "amount": "5000.00",
        "currency": "USD",
    }
    assert made.arguments["lines"][0]["memo"] == ""
    with refused(RequestNotFound):
        await approve_request(
            ApproveRequest(
                request_id=uuid.uuid4(), approver="ann", fingerprint=made.fingerprint
            ),
            ApprovalsUnitOfWork(),
        )
    with refused(InvalidArguments):
        await reject_request(
            RejectRequest(request_id=made.id, approver="ann", reason="x" * 501),
            ApprovalsUnitOfWork(),
        )
    done = await reject_request(
        RejectRequest(request_id=made.id, approver="ann", reason="x" * 500),
        ApprovalsUnitOfWork(),
    )
    assert done.reason == "x" * 500


class Act(Command):
    """What the sample use case does when it runs."""

    kind: str


async def act(command: Act, uow: AccountingUnitOfWork) -> None:
    """Open accounts, or refuse, as ``kind`` says."""
    from domainkit.contrib.approvals.services import _finish
    from domainkit.contrib.messaging import AlreadyHandled
    from domainkit.ddd import ConcurrentUpdate
    from domainkit.money import UnknownCurrency

    match command.kind:
        case "race":
            raise ConcurrentUpdate("Lost a race.")
        case "money":
            raise UnknownCurrency("XXQ")
        case "handled":
            raise AlreadyHandled
        case "nothing":
            return
        case "twice":
            for code in ("t-1", "t-2"):
                await open_account(
                    OpenAccount(
                        code=code, name="T", type=AccountType.ASSET, currency="USD"
                    ),
                    uow,
                )
        case "raced":
            request_id = _running_request()
            await _finish(request_id)  # Another run marked it meanwhile.
            await open_account(
                OpenAccount(
                    code="r-1", name="R", type=AccountType.ASSET, currency="USD"
                ),
                uow,
            )
        case _:
            await open_account(
                OpenAccount(
                    code=command.kind, name="A", type=AccountType.ASSET, currency="USD"
                ),
                uow,
            )


def _running_request() -> uuid.UUID:
    """The id of the request being run."""
    from domainkit.contrib.approvals.guards import _running

    current = _running.get()
    assert current is not None
    return current.request_id  # type: ignore[no-any-return]


acting_guarded = requires_approval("tests.act")(act)


def guard_acts(unit_of_work: Any = AccountingUnitOfWork, command: type = Act) -> None:  # noqa: ANN401 - a factory.
    """Guard the sample use case: one approval of a holder of ``add_account``."""
    guard(
        "tests.act",
        acting_guarded,
        unit_of_work=unit_of_work,
        policies=[Policy("tests.acts", permission="accounting.add_account")],
        command=command,
        domain="alpha",
    )


async def approved_act(kind: str) -> ApprovalRequest:
    """A request to act as ``kind``, approved: its command queued."""
    request = await submit_command("tests.act", Act(kind=kind), maker="maker")
    done = await approve(request, "ann")
    assert done.status is RequestStatus.APPROVED
    return done


async def status_of(request: ApprovalRequest) -> tuple[RequestStatus, str]:
    """The stored request's status and reason."""
    found = await stored(request.id)
    return found.status, found.reason


async def test_a_refused_command_fails_its_request_with_the_refusals_code(
    project: str,
) -> None:
    """Refusals fail the request (the maker may submit again); races are retried."""
    from domainkit.contrib.messaging import AlreadyHandled
    from domainkit.ddd import ConcurrentUpdate

    guard_acts()
    await open_account(
        OpenAccount(code="taken", name="T", type=AccountType.ASSET, currency="USD"),
        AccountingUnitOfWork(),
    )
    taken = await approved_act("taken")
    money = await approved_act("money")
    race = await approved_act("race")
    handled = await approved_act("handled")
    with pytest.raises(ConcurrentUpdate):
        await execute_request(race.id)
    with pytest.raises(AlreadyHandled):
        await execute_request(handled.id)
    await execute_request(taken.id)
    await execute_request(money.id)

    assert await status_of(taken) == (RequestStatus.FAILED, "account_code_taken")
    assert await status_of(money) == (RequestStatus.FAILED, "unknown_currency")
    assert await status_of(race) == (RequestStatus.APPROVED, "")
    assert await status_of(handled) == (RequestStatus.EXECUTED, "")
    assert [action for action, _ in await audited("approvals.request_f")] == [
        "approvals.request_failed",
        "approvals.request_failed",
    ]


async def test_the_request_is_marked_with_the_use_cases_first_commit(
    project: str,
) -> None:
    """On the requests' database, the mark commits with the use case's change.

    A use case that commits twice is marked with the first; one that
    commits nothing is marked after it; one whose request another run
    marked meanwhile is rolled back. Off that database, the request is
    marked after the use case.
    """
    guard_acts()
    twice = await approved_act("twice")
    nothing = await approved_act("nothing")
    raced = await approved_act("raced")
    for request in (twice, nothing, raced):
        await execute_request(request.id)
        assert await status_of(request) == (RequestStatus.EXECUTED, "")
    async with AccountingUnitOfWork() as uow:
        codes = sorted(each.code for each in await uow.accounts.filter())
    assert codes == ["t-1", "t-2"]
    assert len(await audited("approvals.request_executed")) == 3

    from domainkit.contrib.approvals.services import _marking

    assert _marking(object(), uuid.uuid4()) is None


async def test_a_command_that_no_longer_reads_as_approved_never_runs(
    project: str,
) -> None:
    """The guard gone, or its command changed since: the request fails."""
    from domainkit import setup
    from domainkit.apps import registry

    guard_acts()
    gone = await approved_act("gone")
    changed = await approved_act("changed")
    renamed = await approved_act("renamed")

    registry.clear()
    setup()
    await execute_request(gone.id)
    assert await status_of(gone) == (RequestStatus.FAILED, "unknown_action")

    class Wider(Command):
        """The command, with a field it did not have."""

        kind: str
        memo: str = ""

    guard_acts(command=Wider)
    await execute_request(changed.id)
    assert await status_of(changed) == (RequestStatus.FAILED, "invalid_arguments")

    class Other(Command):
        """Another command under the same name."""

        number: int

    registry.clear()
    setup()
    guard_acts(command=Other)
    await execute_request(renamed.id)
    assert await status_of(renamed) == (RequestStatus.FAILED, "invalid_arguments")
    async with AccountingUnitOfWork() as uow:
        assert await uow.accounts.filter() == []


async def test_only_approved_requests_run(project: str) -> None:
    """A pending request's message, sent by hand, runs nothing; a gone one fails."""
    guard_acts()
    pending = await submit_command("tests.act", Act(kind="p"), maker="maker")
    await execute_request(pending.id)
    assert await status_of(pending) == (RequestStatus.PENDING, "")
    with refused(RequestNotFound):
        await execute_request(uuid.uuid4())


async def test_a_guard_that_submits_answers_with_the_request(project: str) -> None:
    """Called directly by someone, it submits for them, and says which request."""
    guard(
        "tests.act",
        acting_guarded,
        unit_of_work=AccountingUnitOfWork,
        policies=[Policy("tests.acts", permission="accounting.add_account")],
        submit=True,
        domain="alpha",
    )
    with acting("maker"), refused(ApprovalPending) as raised:
        await acting_guarded(Act(kind="direct"), AccountingUnitOfWork())
    assert isinstance(raised.value, ApprovalPending)
    assert str(raised.value) == (
        "This command needs approval: it was submitted for approval."
    )
    request = await stored(raised.value.request_id)
    assert (request.maker, request.status) == ("maker", RequestStatus.PENDING)
    assert request.arguments == {"kind": "direct"}
    with acting(None), refused(ApprovalRequired) as anonymous:
        await acting_guarded(Act(kind="direct"), AccountingUnitOfWork())
    assert (
        str(anonymous.value) == "This command needs approval: submit it for approval."
    )


async def test_expiry_works_through_every_overdue_request(
    project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In rounds; a request decided since it was found is left as it is."""
    from domainkit.contrib.approvals import services
    from domainkit.contrib.approvals.uow import RequestRepository

    guard_acts()
    monkeypatch.setattr(services, "EXPIRE_BATCH", 1)
    made = [
        await submit_command("tests.act", Act(kind=f"e-{n}"), maker="maker")
        for n in range(3)
    ]
    await cancel_request(
        CancelRequest(request_id=made[0].id, maker="maker"), ApprovalsUnitOfWork()
    )
    found = RequestRepository.due
    calls: list[int] = []

    async def due(self: RequestRepository, now: Any, limit: int) -> list[Any]:  # noqa: ANN401
        calls.append(limit)
        if len(calls) == 1:
            return [made[0].id]  # Found, then cancelled before it was locked.
        return await found(self, now, limit)

    monkeypatch.setattr(RequestRepository, "due", due)
    with clock.freeze_time(clock.now() + timedelta(days=4)):
        assert await expire_requests() == 2
    assert [(await stored(each.id)).status for each in made] == [
        RequestStatus.CANCELLED,
        RequestStatus.EXPIRED,
        RequestStatus.EXPIRED,
    ]
    assert calls == [1, 1, 1, 1]


async def test_a_command_too_large_to_hold_is_refused(project: str) -> None:
    """Its JSON is held to ``MAX_ARGUMENTS_BYTES``."""
    guard_acts()
    with override_settings(APPROVALS={"MAX_ARGUMENTS_BYTES": 1024}):
        with refused(InvalidArguments):
            await submit_command("tests.act", Act(kind="x" * 1024), maker="maker")
        assert await submit_command("tests.act", Act(kind="x" * 900), maker="maker")


async def test_the_worker_expires_requests_on_its_schedule(project: str) -> None:
    """The periodic task expires what is due."""
    from domainkit.contrib.approvals.tasks import expire_due_requests

    guard_acts()
    request = await submit_command("tests.act", Act(kind="late"), maker="maker")
    with clock.freeze_time(clock.now() + timedelta(days=3)):
        await expire_due_requests()
    assert (await stored(request.id)).status is RequestStatus.EXPIRED


async def test_seals_stop_rows_written_around_the_application(project: str) -> None:
    """A forged decision, or a request whose seal was changed, runs nothing."""
    from domainkit.contrib.approvals.guards import guard_for
    from domainkit.contrib.approvals.services import _rebuild

    guard_vaults(approvals=2)
    half = await submit_command("tests.open_vault", OpenVault(code="v-1"), maker="m")
    await approve(half, "ann")
    async with connections.engine().begin() as connection:
        await connection.execute(
            decisions.insert().values(
                id=uuid.uuid4(),
                request_id=half.id,
                approver="mallory",
                verdict="approve",
                fingerprint=half.fingerprint,
                seal="forged",
                decided_at=clock.now(),
            )
        )
        await connection.execute(
            update(requests)
            .where(requests.c.id == half.id)
            .values(status="approved", approvals=2)
        )
    guard_acts()
    whole = await approved_act("whole")
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(requests).where(requests.c.id == whole.id).values(seal="forged")
        )
    message = r"^The request holds another command than the one approved\.$"
    for request in (half, whole):
        with refused(FingerprintMismatch, message):
            _rebuild(guard_for(request.action), await stored(request.id))
        await execute_request(request.id)
        assert await status_of(request) == (
            RequestStatus.FAILED,
            "fingerprint_mismatch",
        )
    async with AccountingUnitOfWork() as uow:
        assert await uow.accounts.filter() == []


async def test_a_command_rebuilt_otherwise_is_named_as_such(project: str) -> None:
    """The arguments no longer make it, or read back otherwise: said so."""
    from domainkit.contrib.approvals.guards import Guard
    from domainkit.contrib.approvals.services import _rebuild

    guard_acts()
    request = await stored((await approved_act("x")).id)

    class Other(Command):
        number: int

    class Wider(Command):
        kind: str
        memo: str = ""

    def as_(command: type) -> Guard:
        return Guard(
            "tests.act",
            command,
            acting_guarded,
            AccountingUnitOfWork,
            (Policy("tests.acts", permission="a.b"),),
        )

    with refused(InvalidArguments, r"^The arguments do not make the command\.$"):
        _rebuild(as_(Other), request)
    with refused(
        InvalidArguments,
        r"^The command reads otherwise now than when it was approved\.$",
    ):
        _rebuild(as_(Wider), request)


async def test_the_executed_event_names_the_request(
    project: str,
) -> None:
    """``RequestExecuted`` says which request ran, and its action."""
    from domainkit.contrib.approvals import RequestExecuted
    from domainkit.ddd import event_bus

    seen: list[RequestExecuted] = []

    async def keep(event: RequestExecuted) -> None:
        seen.append(event)

    event_bus.subscribe(RequestExecuted, keep)
    try:
        guard_acts()
        request = await approved_act("ran")
        await execute_request(request.id)
    finally:
        event_bus.unsubscribe(RequestExecuted, keep)
    assert [(event.request_id, event.action) for event in seen] == [
        (request.id, "tests.act")
    ]


async def test_a_maker_without_an_id_is_refused(project: str) -> None:
    """Submitted over JSON with an empty maker: said so."""
    with refused(InvalidArguments, r"^A maker's id has 1 to 255 characters\.$"):
        await submit_request(
            SubmitRequest(action=ENTRIES, arguments={}, maker=""),
            ApprovalsUnitOfWork(),
        )
    with refused(InvalidArguments, r"^An approver's id has 1 to 255 characters\.$"):
        await approve_request(
            ApproveRequest(request_id=uuid.uuid4(), approver="", fingerprint="0"),
            ApprovalsUnitOfWork(),
        )
