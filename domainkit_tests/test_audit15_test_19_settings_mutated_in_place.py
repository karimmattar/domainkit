"""TEST-19: a setting changed in place through ``settings`` stays for the run.

The plugin's ``settings`` fixture reads ``settings.CACHES`` from the current
settings object, and the mappings settings hold (``CACHES``, ``DATABASES``,
``LOGGING``, every app's block such as ``AUTH`` or ``KEYS``) are plain
dicts. ``settings.CACHES["reports"] = {...}``, or
``settings.AUTH["MFA"] = ...``, the way pytest-django users write it,
changes the session's settings object itself: nothing validates it, and
nothing puts it back, so every later test runs with it, in whatever order
pytest-randomly chose. The docs promise each change "validated, until the
test ends". Fails on ``d126394``.
"""

import pytest

from domainkit.conf import override_settings
from domainkit.testing import IN_MEMORY
from domainkit_tests.test_testing import prefix, run_plugin  # noqa: F401 - fixture.


def test_a_mapping_changed_in_place_is_put_back_or_refused(
    prefix: str,  # noqa: F811 - the fixture imported above.
    pytester: pytest.Pytester,
) -> None:
    """The next test sees the settings as they were."""
    pytester.makepyfile(
        test_in_place="""
        import pytest

        def test_one(settings):
            try:
                settings.CACHES["reports"] = {"URL": "memory://"}
            except TypeError:
                pytest.skip("Refused: the mapping is read-only, as it should be.")

        def test_two(settings):
            assert "reports" not in settings.CACHES
        """
    )
    with override_settings(DATABASES={"default": {"URL": IN_MEMORY}}):
        result = run_plugin(pytester)

    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0, result.stdout.str()
