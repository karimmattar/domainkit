"""A guard's ``maker_rule`` is checked again when its request runs (SEC-16b-7)."""

from fastapi.testclient import TestClient

from domainkit.contrib.approvals.services import _check_maker
from domainkit.management.base import run_async
from domainkit_tests.test_approvals_api import (  # noqa: F401 - the fixture.
    _id,
    client,
)
from domainkit_tests.test_auth_http import create_user
from domainkit_tests.test_sec16b_7_maker_rule_not_checked_at_run import STAFF_ONLY


def test_a_maker_who_still_passes_the_maker_rule_runs(
    client: TestClient,  # noqa: F811 - the fixture.
) -> None:
    """Staff, as when they submitted: the rule passes on a run's connection."""
    create_user("still-staff", is_staff=True)

    run_async(_check_maker(STAFF_ONLY, str(_id("still-staff"))))
