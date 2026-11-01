"""TEST-15: the ``settings`` fixture does not reach what ``ready()`` built.

``registry.settings(KeysSettings)`` follows ``override_settings`` (it is
re-validated for each settings object), and the plugin's ``settings``
fixture promises that "the setting changes, validated, until the test
ends". But ``KeysConfig.ready()`` builds the process's key ring once
(``use_keyring(ring_for(...))``), so a project test that rotates keys with
``settings.KEYS = {...}`` still seals under the startup key, while
``keys.W001`` and ``keys rewrap`` read the new ``DATA_KEYS``: one test sees
two key configurations. Approvals' ``ENTRY_THRESHOLDS`` are captured the
same way (``guard_entries(config)`` in ``ready()``). In domainkit's own
suite every test runs ``setup()`` again, so its tests never meet this.
Fails on ``d126394``.
"""

import pytest

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.keys.settings import KeysSettings
from domainkit.keys.ring import keyring
from domainkit.management.base import run_async
from domainkit.testing import IN_MEMORY
from domainkit_tests.conftest import ALPHA, CONTENTTYPES
from domainkit_tests.test_testing import prefix, run_plugin  # noqa: F401 - fixture.


def test_the_settings_fixture_rotates_the_key_ring(
    prefix: str,  # noqa: F811 - the fixture imported above.
    pytester: pytest.Pytester,
) -> None:
    """After ``settings.KEYS`` names a new current key, new values use it."""
    pytester.makeconftest(
        """
        import pytest

        from domainkit import setup

        @pytest.fixture(scope="session", autouse=True)
        def domains():
            setup()  # As a project's conftest, or its first client, loads them.
        """
    )
    pytester.makepyfile(
        test_rotation="""
        from domainkit.apps import registry
        from domainkit.contrib.keys.settings import KeysSettings
        from domainkit.keys.ring import keyring
        from domainkit.management.base import run_async

        def test_rotate(settings):
            made = run_async(keyring().provider.generate_data_key())
            settings.KEYS = {"DATA_KEYS": {"k2": made.wrapped}, "CURRENT_KEY": "k2"}
            assert registry.settings(KeysSettings).current == "k2"
            assert keyring().current == "k2"
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        INSTALLED_DOMAINS=[CONTENTTYPES, "domainkit.contrib.keys", ALPHA],
    ):
        result = run_plugin(pytester)

    result.assert_outcomes(passed=1)


def test_the_ring_is_the_settings_ring_after_an_override() -> None:
    """The same, without pytester: an override of ``KEYS`` reaches ``keyring()``."""
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        INSTALLED_DOMAINS=[CONTENTTYPES, "domainkit.contrib.keys", ALPHA],
    ):
        from domainkit import setup

        setup()
        made = run_async(keyring().provider.generate_data_key())
        with override_settings(
            KEYS={"DATA_KEYS": {"k2": made.wrapped}, "CURRENT_KEY": "k2"}
        ):
            assert registry.settings(KeysSettings).current == "k2"
            assert keyring().current == "k2"
