"""SEC-16b-7: a guard's ``maker_rule`` is not checked again when its request runs.

FIN-93: "before running, resolve the maker as a principal and require it
to be active and to hold the guard's maker ``permission`` and
``maker_rule``". ``_check_maker`` checks that the maker is an active user
holding ``guard.permission`` only. A guard that names its makers by an
access rule (``maker_rule``: staff, a role, a verified customer; the
documented way, which the superuser guard uses) runs the command of a
maker who no longer passes the rule: a staff member demoted (or a
customer whose verification was revoked) after submitting still has the
command run as them once approved. Only ``grant_superuser`` checks its
own rule, in its service.

The test asserts the safe behaviour (``maker_not_allowed`` for an
active user who no longer passes the maker rule) and fails on
``675dfd6``.
"""

import pytest
from fastapi.testclient import TestClient

from domainkit.contrib.approvals import MakerNotAllowed, Policy, requires_approval
from domainkit.contrib.approvals.guards import Guard
from domainkit.contrib.approvals.services import _check_maker
from domainkit.contrib.auth.access import IsAdminUser
from domainkit.ddd import Command
from domainkit.management.base import run_async
from domainkit_tests.test_approvals_api import (  # noqa: F401 - the fixture.
    _id,
    client,
    user,
)


class Payout(Command):
    """A payout only staff may ask for."""

    amount: str


@requires_approval("tests.staff_payout")
async def payout(command: Payout, uow: object) -> None:
    """Pay out (nothing, here)."""


STAFF_ONLY = Guard(
    "tests.staff_payout",
    Payout,
    payout,
    object,
    (Policy("tests.staff_payout", approvals=2, permission="tests.approve"),),
    maker_rule=IsAdminUser(),
)


def test_a_maker_who_no_longer_passes_the_maker_rule_is_refused_at_run(
    client: TestClient,
) -> None:
    """An active user, not (or no longer) staff: the run is refused."""
    user("demoted")  # Active, and not staff: as one demoted since submitting.

    with pytest.raises(MakerNotAllowed):
        run_async(_check_maker(STAFF_ONLY, str(_id("demoted"))))
