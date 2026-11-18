"""``check`` names the installed apps whose rows every tenant shares (ADR 0183).

With ``TENANCY`` set, ``tenancy.I001`` names ``domainkit.contrib.accounting``
and ``domainkit.contrib.limits`` where they are installed: an info, which
never fails ``check --deploy``. Without ``TENANCY``, or without them, it
says nothing.
"""

import io
from collections.abc import Iterator

import pytest

from domainkit.checks import CheckMessage, Level
from domainkit.conf import override_settings
from domainkit.management import call_command
from domainkit.tenancy import shared_apps
from domainkit_tests.conftest import Migrated
from domainkit_tests.test_tenancy_everywhere import LIMITS

EXPECTED = (
    "TENANCY is set, and domainkit.contrib.accounting (the ledger) and "
    "domainkit.contrib.limits (the limits) keep rows that belong to no tenant: "
    "every tenant shares them. Their routes count only the permissions a user "
    "holds everywhere, never a member's role."
)


@pytest.fixture
def project(migrated: Migrated) -> Iterator[None]:
    """A project with limits and the ledger, served per tenant."""
    with migrated(LIMITS):
        yield


@pytest.mark.usefixtures("project")
def test_the_apps_every_tenant_shares_are_named() -> None:
    """An info naming both, with a hint; nothing without ``TENANCY``."""
    [found] = shared_apps()

    assert isinstance(found, CheckMessage)
    assert (found.id, found.level) == ("tenancy.I001", Level.INFO)
    assert found == EXPECTED
    assert "docs/tenancy.md, 'Rows of no tenant'" in found.hint
    with override_settings(TENANCY=None):
        assert shared_apps() == []


@pytest.mark.usefixtures("project")
def test_check_lists_the_info_and_counts_no_warning() -> None:
    """``check`` shows it as information: never a warning, so never refused."""
    output = io.StringIO()
    call_command("check", stdout=output)

    shown = output.getvalue()
    assert f"  Info: tenancy.I001: {EXPECTED}\n" in shown
    assert "tenancy.I001" not in shown.partition("\n")[0]


def test_a_project_without_those_apps_hears_nothing() -> None:
    """``TENANCY`` alone names nothing: no installed app keeps such rows."""
    with override_settings(
        INSTALLED_DOMAINS=["domainkit.contrib.contenttypes"],
        TENANCY={"HEADER": "X-Tenant"},
    ):
        assert shared_apps() == []
