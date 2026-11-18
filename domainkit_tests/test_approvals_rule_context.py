"""Approvals asks each maker and approver rule on a request context of its own.

When an approved command runs, its maker is asked again, and approvers are
looked for, through rules that see the user on a connection of no
credentials. Each call gets its own, so nothing a rule keeps on it (a
memo in ``request.state``) reaches another call, and its scope is
complete enough to read.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from domainkit.contrib.approvals import (
    MakerNotAllowed,
    Policy,
    approvers_of,
    guard,
    requires_approval,
)
from domainkit.contrib.approvals.guards import Guard
from domainkit.contrib.approvals.services import _check_maker
from domainkit.ddd import Command
from domainkit.http.access import BasePermission
from domainkit.management.base import run_async
from domainkit_tests.test_approvals_viewers import (  # noqa: F401 - the fixture.
    client,
    person,
    submitted,
)


class Memo(Command):
    """A command whose rules are asked about each user."""

    note: str


@requires_approval("tests.rule_context")
async def memo(command: Memo, uow: object) -> None:
    """Nothing: only its rules are asked."""


class KeepsItsAnswer(BasePermission):
    """A rule that keeps its answer in ``request.state``, as a route's may.

    It lets through the user named ``allowed``.
    """

    async def has_permission(self, request: Any, user: Any) -> bool:  # noqa: ANN401
        """The answer kept on this request, else the user's, kept."""
        kept = getattr(request.state, "answer", None)
        if kept is None:
            kept = request.state.answer = user.username == "allowed"
        return bool(kept)


class ReadsTheContext(BasePermission):
    """A rule that reads its request's query parameters and ``app.state``."""

    async def has_permission(self, request: Any, user: Any) -> bool:  # noqa: ANN401
        """True when there are no query parameters and ``app.state`` is empty."""
        return dict(request.query_params) == {} and vars(request.app.state) == {
            "_state": {}
        }


def made_by(rule: BasePermission) -> Guard:
    """A guard of ``memo`` whose maker must pass ``rule``."""
    return Guard(
        "tests.rule_context",
        Memo,
        memo,
        object,
        (Policy("tests.rule_context", permission="accounting.add_account"),),
        maker_rule=rule,
    )


def test_each_maker_rule_call_answers_afresh(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """The user the rule allows, then the one it refuses: allowed, then refused."""
    rule = KeepsItsAnswer()
    allowed = str(person("allowed"))
    refused = str(person("refused"))

    run_async(_check_maker(made_by(rule), allowed))
    with pytest.raises(MakerNotAllowed):
        run_async(_check_maker(made_by(rule), refused))


def test_each_approver_rule_call_answers_afresh(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Among staff the approver rule is asked about, only the one it allows."""
    guard(
        "tests.rule_context",
        memo,
        unit_of_work=object,
        policies=[Policy("tests.rule_context", approver_rule=KeepsItsAnswer())],
        domain="alpha",
    )
    person("allowed", is_staff=True)
    person("refused", is_staff=True)
    request = submitted("tests.rule_context", Memo(note="x"), person("maker"))

    found = run_async(approvers_of(request))

    assert [each.username for each in found] == ["allowed"]


def test_a_rule_reads_no_query_parameters_and_an_empty_app_state(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Neither raises: the connection a rule is asked on has a complete scope."""
    run_async(_check_maker(made_by(ReadsTheContext()), str(person("reader"))))
