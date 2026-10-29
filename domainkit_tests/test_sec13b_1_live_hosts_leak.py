"""SEC-13b-1: ``live_server``'s ``127.0.0.1`` stays in ``ALLOWED_HOSTS`` after its test.

``override_settings`` puts back the settings it found when it was entered,
not the ones beneath it, so overrides must end in reverse order. The
``live_server`` fixture holds one for the whole test; a ``settings``
fixture requested before it, changed in the test's body, ends after it and
puts back the settings it found: those of ``live_server``. Every later test
of the run, and the server it builds, then accepts ``Host: 127.0.0.1``.
"""

from collections.abc import Iterator

import pytest

# Loaded before pytester takes its snapshot of the loaded modules (see
# test_testing.py): the routes define tables, Alembic keeps global state.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_auth_routes  # noqa: F401
from domainkit.conf import override_settings
from domainkit.testing import IN_MEMORY
from domainkit_tests.conftest import MIDDLEWARE
from domainkit_tests.test_testing import prefix, run_plugin

__all__ = ["prefix"]


@pytest.fixture
def auth_settings(auth_project: str) -> Iterator[None]:
    """The inner session's project: auth, on a database the plugin makes."""
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        yield


def test_the_live_server_s_host_ends_with_its_test(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """A test that changes a setting beside ``live_server`` leaves no host behind."""
    pytester.makepyfile(
        test_hosts="""
        from domainkit.conf import settings as current

        BEFORE = []

        def test_records_the_hosts():
            BEFORE.append(list(current.ALLOWED_HOSTS))
            assert "127.0.0.1" not in current.ALLOWED_HOSTS

        def test_serves_and_changes_a_setting(settings, live_server):
            settings.DEBUG = not current.DEBUG

        def test_hosts_are_back():
            assert list(current.ALLOWED_HOSTS) == BEFORE[0]
        """
    )

    run_plugin(pytester).assert_outcomes(passed=3)
