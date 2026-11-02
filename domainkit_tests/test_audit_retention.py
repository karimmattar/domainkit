"""How long the audit trail is kept: ``AUDIT.MIN_RETENTION``, and the PCI preset's year.

PCI DSS v4.0 10.5.1 keeps audit logs twelve months: under
``AUTH.PRESET = "pci-dss-4"`` the trail is kept a year at least,
whatever ``MIN_RETENTION`` says; elsewhere its default stays 30 days (FIN-95).
"""

from collections.abc import Iterator
from datetime import timedelta

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.audit.settings import PCI_RETENTION, min_retention
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES

AUDIT = "domainkit.contrib.audit"


@pytest.fixture(autouse=True)
def cleared() -> Iterator[None]:
    """Each case sets the domains up afresh, and leaves none behind."""
    registry.clear()
    yield
    registry.clear()


@pytest.mark.parametrize(
    ("domains", "auth", "audit", "kept"),
    [
        ([AUDIT, ALPHA], {}, {}, timedelta(days=30)),
        ([CONTENTTYPES, AUTH, AUDIT, ALPHA], {}, {}, timedelta(days=30)),
        (
            [CONTENTTYPES, AUTH, AUDIT, ALPHA],
            {"PRESET": "pci-dss-4"},
            {},
            PCI_RETENTION,
        ),
        (
            [CONTENTTYPES, AUTH, AUDIT, ALPHA],
            {"PRESET": "pci-dss-4"},
            {"MIN_RETENTION": timedelta(days=400)},
            timedelta(days=400),
        ),
    ],
)
def test_the_pci_preset_keeps_the_trail_a_year_at_least(
    domains: list[str],
    auth: dict[str, object],
    audit: dict[str, object],
    kept: timedelta,
) -> None:
    """The setting, raised to 365 days under the preset; never lowered."""
    given = {"AUTH": auth} if AUTH in domains else {}
    with override_settings(INSTALLED_DOMAINS=domains, AUDIT=audit, **given):
        setup()
        assert min_retention() == kept
    assert timedelta(days=365) == PCI_RETENTION
