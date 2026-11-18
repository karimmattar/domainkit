"""Access rules asked outside a request: ``RuleContext`` and ``OfflineContext``.

A28-ARCH-08. The rules read a Starlette connection. Approvals asked a
guard's maker rule and a policy's approver rule on one connection the
whole process shared, whose scope had no ``query_string`` or ``app`` and
no user: a rule written for routes raised there instead of answering, the
run was tried again until it was dead-lettered, ``approvers_of`` raised
for every caller, and what a rule kept in ``request.state`` was seen by
every later call. Each call is now a whole connection of its own, and a
rule that raises refuses, logged (ADR 0196).
"""

import logging
from typing import Any, ClassVar, Final
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.requests import HTTPConnection

from domainkit.contrib.approvals import (
    MakerNotAllowed,
    Policy,
    RequestStatus,
    approvers_of,
    guard,
    requires_approval,
)
from domainkit.contrib.approvals.guards import Guard
from domainkit.contrib.approvals.services import _check_maker
from domainkit.contrib.messaging.testing import drain
from domainkit.ddd import Command, DomainError
from domainkit.documents.base import RendererNotConfigured
from domainkit.exceptions import ServiceBusy, UpstreamError
from domainkit.http.access import (
    AnonymousPrincipal,
    BasePermission,
    HasScope,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
    IsUnscoped,
    OfflineContext,
    RuleContext,
    scopes_of,
)
from domainkit.http.client import CallTimeout, CircuitOpen, ResponseTooLarge
from domainkit.http.errors import transient
from domainkit.http.guard import UnsafeURL
from domainkit.management.base import run_async
from domainkit.storage.s3 import WriteOnceIgnored
from domainkit_tests.test_approvals_api import decide
from domainkit_tests.test_approvals_tenancy import stored
from domainkit_tests.test_approvals_viewers import (  # noqa: F401 - the fixture.
    client,
    person,
    submitted,
)
from domainkit_tests.test_auth_admin import token_for

HOLDS: Final = "accounting.add_account"
"""The permission the guards' policies name their approvers by."""
RAISED: Final = (
    "The access rule Raises raised LookupError asked outside a request; it refuses"
)
"""What is logged of a rule that raised: its class and the error's, not its text."""


class Ruled(Command):
    """What the guarded use cases below are asked to do."""

    note: str


@requires_approval("tests.ruled")
async def ruled(command: Ruled, uow: object) -> None:
    """Do nothing: a use case whose maker a rule of the project's checks."""


@requires_approval("tests.ruled_by_a_broken_rule")
async def broken(command: Ruled, uow: object) -> None:
    """Do nothing: a use case whose maker rule raises."""


class ReadsTheCall(BasePermission):
    """A rule written for routes: it reads what a request carries, and keeps state.

    It allows a call once: a ``state`` another call kept refuses it.
    """

    read: ClassVar[list[tuple[object, ...]]] = []

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:  # noqa: ANN401 - a user.
        """Read the query, the application, the user, the state; allow once."""
        ReadsTheCall.read.append(
            (
                dict(request.query_params),
                dict(request.headers),
                request.app.state is not None,
                request.user is user,
                request.auth,
                request.url.path,
                request.scope["method"],
            )
        )
        if getattr(request.state, "asked", False):
            return False  # Another call's state: never seen.
        request.state.asked = True
        return True


class Raises(BasePermission):
    """A rule with a mistake of its own: it raises whatever it is asked."""

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:  # noqa: ANN401 - a user.
        """Raise."""
        raise LookupError("a mistake in the rule")


class OnlyActive(BasePermission):
    """A rule that says it reads only what every context has (``RuleContext``)."""

    async def has_permission(self, request: RuleContext, user: Any) -> bool:  # noqa: ANN401 - a user.
        """Active users, on a call that keeps its own state."""
        request.state.asked = True
        return bool(user.is_active) and request.scope["user"] is user


class Suspended(DomainError):
    """A refusal of the project's own, with its own code."""


class RefusesInItsOwnWords(BasePermission):
    """A maker rule that refuses with a domain error of the project's."""

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:  # noqa: ANN401 - a user.
        """Refuse, saying why."""
        raise Suspended("This maker is suspended.")


class LosesItsConnectionOnce(BasePermission):
    """A rule whose own call to another service fails once, then answers."""

    failures: ClassVar[list[BaseException]] = []
    """What it raises next, one call each; then it allows."""

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:  # noqa: ANN401 - a user.
        """Raise what is left to raise, then allow."""
        if LosesItsConnectionOnce.failures:
            raise LosesItsConnectionOnce.failures.pop(0)
        return True


def made_by(maker_rule: BasePermission, service: Any = ruled) -> Guard:  # noqa: ANN401 - a service.
    """A guard of ``service`` whose makers ``maker_rule`` checks."""
    action = "tests.ruled" if service is ruled else "tests.ruled_by_a_broken_rule"
    return Guard(
        action,
        Ruled,
        service,
        object,
        (Policy(action, permission=HOLDS),),
        maker_rule=maker_rule,
    )


def test_an_offline_context_is_a_whole_call_of_its_own() -> None:
    """No client, no query, headers, cookies, or session data; its own state."""
    user = AnonymousPrincipal()
    first = OfflineContext(user, method="DELETE", path="/tasks/purge")
    second = OfflineContext(user)
    first.state.kept = "first's"

    assert (first.scope["method"], first.url.path) == ("DELETE", "/tasks/purge")
    assert (second.scope["method"], second.url.path) == ("POST", "/")
    assert dict(first.query_params) == {}
    assert dict(first.headers) == {}
    assert first.cookies == {}
    assert first.session == {}
    assert first.path_params == {}
    assert first.client is None
    assert first.user is user
    assert first.auth is None
    assert scopes_of(first) is None
    assert not hasattr(second.state, "kept")
    assert first.app is not second.app
    assert not hasattr(first.app.state, "kept")


def test_an_offline_context_may_be_limited_to_scopes() -> None:
    """``scopes`` limits its credentials, as a scoped token limits a request's."""
    scoped = OfflineContext(AnonymousPrincipal(), scopes=["shop.view_order"])

    assert scopes_of(scoped) == frozenset({"shop.view_order"})
    assert scoped.auth.scopes == frozenset({"shop.view_order"})


class Known:
    """A principal who proved who they are, holding nothing."""

    id = uuid4()
    is_authenticated = True
    is_active = True

    async def ahas_perms(self, perms: object, obj: object = None) -> bool:
        """Hold nothing."""
        return False


@pytest.mark.anyio
async def test_the_built_in_rules_answer_on_an_offline_context() -> None:
    """They read only what ``RuleContext`` names: the method, the scopes, the user."""
    user = Known()
    unscoped = OfflineContext(user)
    scoped = OfflineContext(user, method="GET", scopes=["shop.view_order"])

    assert await IsAuthenticated().has_permission(unscoped, user)
    assert await IsUnscoped().has_permission(unscoped, user)
    assert not await IsUnscoped().has_permission(scoped, user)
    assert await HasScope("shop.view_order").has_permission(scoped, user)
    assert not await HasScope("shop.add_order").has_permission(scoped, user)
    anonymous = AnonymousPrincipal()
    assert await IsAuthenticatedOrReadOnly().has_permission(
        OfflineContext(anonymous, method="GET"), anonymous
    )
    assert not await IsAuthenticatedOrReadOnly().has_permission(
        OfflineContext(anonymous), anonymous
    )


@pytest.mark.anyio
async def test_a_rule_written_for_any_context_answers_a_request_as_offline() -> None:
    """An ``HTTPConnection`` is a ``RuleContext``, and so is an offline one."""
    user = Known()
    request: RuleContext = HTTPConnection(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "user": user}
    )
    offline: RuleContext = OfflineContext(user)

    assert await OnlyActive().has_permission(request, user)
    assert await OnlyActive().has_permission(offline, user)
    assert request.state.asked
    assert offline.state.asked


def test_a_maker_rule_reads_a_whole_call_of_its_own(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Query, application, user, state: as on a route, a new one each run."""
    maker = str(person("maker"))
    ReadsTheCall.read.clear()
    checked = made_by(ReadsTheCall())

    run_async(_check_maker(checked, maker))
    run_async(_check_maker(checked, maker))  # Not refused by the first's state.

    (query, headers, has_app, is_user, auth, path, method), again = ReadsTheCall.read
    assert (query, headers, has_app, is_user, auth) == ({}, {}, True, True, None)
    assert (path, method) == ("/domainkit.approvals.execute", "POST")
    assert again == ReadsTheCall.read[0]


def test_a_maker_rule_that_raises_refuses_the_run_and_is_logged(
    client: TestClient,  # noqa: F811 - the fixture.
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its error is the rule's mistake: the maker may not make the command."""
    maker = str(person("maker"))

    with (
        caplog.at_level(logging.WARNING, "domainkit.contrib.approvals.services"),
        pytest.raises(MakerNotAllowed),
    ):
        run_async(_check_maker(made_by(Raises(), broken), maker))

    (line,) = [
        record.getMessage()
        for record in caplog.records
        if record.name == "domainkit.contrib.approvals.services"
    ]
    assert line == RAISED
    assert "a mistake in the rule" not in caplog.text


def test_an_approved_command_whose_maker_rule_raises_fails_once(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """The run fails the request (``maker_not_allowed``): it is not tried again."""
    guard(
        "tests.ruled_by_a_broken_rule",
        broken,
        unit_of_work=object,
        policies=[Policy("tests.ruled_by_a_broken_rule", permission=HOLDS)],
        maker_rule=Raises(),
        domain="alpha",
    )
    maker = person("maker")
    person("holder", HOLDS)
    made = submitted("tests.ruled_by_a_broken_rule", Ruled(note="x"), maker)
    shown = {"id": str(made.id), "fingerprint": made.fingerprint}
    assert decide(client, shown, token_for("holder")).json()["status"] == "approved"

    drained = run_async(drain())

    done = stored(made.id)
    assert drained.failures == []
    assert (done.status, done.reason) == (RequestStatus.FAILED, "maker_not_allowed")


def test_whom_to_tell_is_found_by_a_rule_written_for_routes(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """``approvers_of`` asks the approver rule on a call of its own per user."""
    guard(
        "tests.ruled",
        ruled,
        unit_of_work=object,
        policies=[Policy("tests.by_rule", approver_rule=ReadsTheCall())],
        domain="alpha",
    )
    maker = person("maker")
    person("staffer", is_staff=True)
    person("root", is_superuser=True)
    made = submitted("tests.ruled", Ruled(note="x"), maker)
    ReadsTheCall.read.clear()

    found = run_async(approvers_of(made))

    assert sorted(user.username for user in found) == ["root", "staffer"]
    assert len(ReadsTheCall.read) == 2


def test_an_approver_rule_that_raises_lets_no_one_through(
    client: TestClient,  # noqa: F811 - the fixture.
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nobody is told, and the rule is logged once, not once per user."""
    guard(
        "tests.ruled_by_a_broken_rule",
        broken,
        unit_of_work=object,
        policies=[Policy("tests.by_a_broken_rule", approver_rule=Raises())],
        domain="alpha",
    )
    maker = person("maker")
    person("staffer", is_staff=True)
    person("root", is_superuser=True)
    made = submitted("tests.ruled_by_a_broken_rule", Ruled(note="x"), maker)

    with caplog.at_level(logging.WARNING, "domainkit.contrib.approvals.services"):
        found = run_async(approvers_of(made))

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "domainkit.contrib.approvals.services"
    ]
    assert found == []
    assert lines[0] == RAISED
    assert lines.count(RAISED) == 1  # The other user's is held back, counted.


def test_transient_errors_are_told_apart_from_a_rules_own() -> None:
    """A timeout, a connection, a busy or unreachable database: try again."""
    gone = OperationalError("SELECT 1", {}, Exception("gone"))
    gone.connection_invalidated = True

    assert transient(TimeoutError())
    assert transient(ConnectionResetError())
    assert transient(ServiceBusy("Busy."))
    assert transient(UpstreamError())
    assert transient(CircuitOpen("Calls to the payments service fail at once."))
    assert transient(gone)
    assert transient(DBAPIError("SELECT 1", {}, Stated("57014")))  # A timeout.
    assert not transient(DBAPIError("SELECT 1", {}, Stated("22021")))  # A NUL.
    assert not transient(OperationalError("SELECT 1", {}, Exception("syntax")))
    assert not transient(LookupError("a mistake"))
    assert not transient(Suspended("No."))


def raised_from(error: Exception, cause: BaseException) -> Exception:
    """``error``, as if raised from ``cause``."""
    error.__cause__ = cause
    return error


def answered(status_code: int) -> httpx2.HTTPStatusError:
    """What ``raise_for_status`` raises for an answer of ``status_code``."""
    request = httpx2.Request("GET", "https://service.example/v1")
    return httpx2.HTTPStatusError(
        "refused",
        request=request,
        response=httpx2.Response(status_code, request=request),
    )


def test_an_outgoing_call_is_tried_again_only_where_it_may_succeed() -> None:
    """A timeout, a 5xx, an open circuit: yes. A size, a 4xx, a refusal: no."""
    deep: BaseException = answered(403)
    for _ in range(20):  # Read 16 deep at most: a 4xx past that is not seen.
        deep = raised_from(ConnectionResetError(), deep)

    assert transient(CallTimeout("slow"))
    assert transient(raised_from(UpstreamError(), answered(503)))
    assert transient(raised_from(UpstreamError(), answered(429)))
    assert transient(raised_from(UpstreamError(), answered(408)))
    assert transient(raised_from(UpstreamError(), deep))
    assert not transient(ResponseTooLarge("Too long."))
    assert not transient(WriteOnceIgnored("The store ignores If-None-Match."))
    assert not transient(RendererNotConfigured("No renderer."))
    assert not transient(raised_from(UpstreamError(), answered(403)))
    assert not transient(raised_from(UpstreamError(), UnsafeURL("Private.")))


def test_a_maker_rule_whose_answer_is_too_large_refuses(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Trying again cannot change an answer's size: the maker is refused."""
    maker = str(person("maker"))
    LosesItsConnectionOnce.failures[:] = [ResponseTooLarge("Too long.")]

    with pytest.raises(MakerNotAllowed):
        run_async(_check_maker(made_by(LosesItsConnectionOnce()), maker))


class Stated(Exception):
    """A driver's error with a SQLSTATE, as asyncpg raises one."""

    def __init__(self, sqlstate: str) -> None:
        """Carry ``sqlstate``."""
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("slow"), ConnectionRefusedError("refused"), UpstreamError()],
)
def test_a_transient_error_of_a_maker_rule_is_raised_not_refused(
    client: TestClient,  # noqa: F811 - the fixture.
    failure: Exception,
) -> None:
    """The run is tried again: a rule that could not answer has not refused."""
    maker = str(person("maker"))
    LosesItsConnectionOnce.failures[:] = [failure]

    with pytest.raises(type(failure)):
        run_async(_check_maker(made_by(LosesItsConnectionOnce()), maker))
    run_async(_check_maker(made_by(LosesItsConnectionOnce()), maker))  # Allowed.


def test_an_approved_command_whose_maker_rule_lost_its_connection_runs_again(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """The first run is tried again, and the second runs the command."""
    guard(
        "tests.ruled",
        ruled,
        unit_of_work=object,
        policies=[Policy("tests.ruled", permission=HOLDS)],
        maker_rule=LosesItsConnectionOnce(),
        domain="alpha",
    )
    maker = person("maker")
    person("holder", HOLDS)
    made = submitted("tests.ruled", Ruled(note="x"), maker)
    shown = {"id": str(made.id), "fingerprint": made.fingerprint}
    LosesItsConnectionOnce.failures[:] = [ConnectionResetError("reset")]
    assert decide(client, shown, token_for("holder")).json()["status"] == "approved"

    drained = run_async(drain(delayed=True))

    assert len(drained.failures) == 1  # The first run, tried again.
    assert stored(made.id).status is RequestStatus.EXECUTED


def test_a_domain_error_of_a_maker_rule_fails_the_run_with_its_code(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """As in 2.8: the rule's own refusal names why, not ``maker_not_allowed``."""
    guard(
        "tests.ruled",
        ruled,
        unit_of_work=object,
        policies=[Policy("tests.ruled", permission=HOLDS)],
        maker_rule=RefusesInItsOwnWords(),
        domain="alpha",
    )
    maker = person("maker")
    person("holder", HOLDS)
    made = submitted("tests.ruled", Ruled(note="x"), maker)
    shown = {"id": str(made.id), "fingerprint": made.fingerprint}
    assert decide(client, shown, token_for("holder")).json()["status"] == "approved"

    with pytest.raises(Suspended):
        run_async(_check_maker(made_by(RefusesInItsOwnWords()), str(maker)))
    drained = run_async(drain())

    done = stored(made.id)
    assert drained.failures == []
    assert (done.status, done.reason) == (RequestStatus.FAILED, "suspended")


@pytest.mark.parametrize(
    ("rule", "raised"),
    [(LosesItsConnectionOnce(), True), (RefusesInItsOwnWords(), False)],
)
def test_whom_to_tell_is_not_guessed_while_a_rule_cannot_answer(
    client: TestClient,  # noqa: F811 - the fixture.
    rule: BasePermission,
    raised: bool,
) -> None:
    """A transient error goes up; a domain error leaves the user out."""
    guard(
        "tests.ruled",
        ruled,
        unit_of_work=object,
        policies=[Policy("tests.by_rule", approver_rule=rule)],
        domain="alpha",
    )
    maker = person("maker")
    person("staffer", is_staff=True)
    made = submitted("tests.ruled", Ruled(note="x"), maker)
    LosesItsConnectionOnce.failures[:] = [ConnectionAbortedError("aborted")]

    if raised:
        with pytest.raises(ConnectionAbortedError):
            run_async(approvers_of(made))
        assert [user.username for user in run_async(approvers_of(made))] == ["staffer"]
    else:
        assert run_async(approvers_of(made)) == []
