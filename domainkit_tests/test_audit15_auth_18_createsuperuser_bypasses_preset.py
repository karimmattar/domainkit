"""AUTH-18: ``createsuperuser`` offers to bypass the validators, PCI preset or not.

Asked interactively, a password the validators refuse is kept on a ``y``
(Django's prompt), whatever ``AUTH.PASSWORD_PRESET`` says and wherever the
command runs. Under ``"pci-dss-4"``, whose startup check (``auth.E010``)
refuses validators that take passwords under 12 characters (8.3.6), the
most powerful account is made with ``1234``; it is not temporary either,
so it is never changed (8.3.5), unlike ``changepassword`` (SEC-15c-3).
"""

import io

from domainkit.conf import override_settings
from domainkit.contrib.auth import check_password
from domainkit.contrib.auth.management.commands import createsuperuser
from domainkit.management import call_command
from domainkit_tests.conftest import FAST_HASHER
from domainkit_tests.test_auth_commands import (
    Prompts,
    prompts,  # noqa: F401 - the fixture.
    stored,
)


def test_the_pci_preset_admits_no_bypassed_superuser_password(
    auth_project: str,
    prompts: Prompts,  # noqa: F811 - the fixture.
) -> None:
    """Under the preset, a refused password is asked again, never kept."""
    prompts(
        createsuperuser.Command,
        plain=["admin", "admin@example.com", "y"],
        secret=["1234", "1234", "Str0ng-enough-pass", "Str0ng-enough-pass"],
    )
    with override_settings(
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER], "PASSWORD_PRESET": "pci-dss-4"}
    ):
        call_command("createsuperuser", stdout=io.StringIO(), stderr=io.StringIO())
        admin = stored("admin")

    assert admin is not None
    assert not check_password("1234", admin.password.value), (
        "a superuser was made with a password the PCI preset refuses"
    )
