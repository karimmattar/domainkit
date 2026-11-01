"""TEST-14: a hook a test registers stays for every later test.

``HookPoint.__call__`` lets a test register a hook for an installed domain
(``on_guard(fn, domain="payments")``, "a test, a command"), and the
plugin's docs promise that "the subscriptions, tasks, metric refreshers,
and currencies the test added are dropped". Hooks are not among them: the
plugin keeps no hook point (``KEPT`` in ``domainkit/testing/__init__.py``),
and no public reset exists for one, so a guard, an erasure hook, a
deployment warning or a startup hook one test adds runs in every test after
it, in whatever order pytest-randomly picked. Fails on ``d126394``.
"""

from pathlib import Path

import pytest

from domainkit.conf import override_settings
from domainkit.testing import IN_MEMORY
from domainkit_tests.conftest import MIDDLEWARE
from domainkit_tests.test_testing import prefix, run_plugin  # noqa: F401 - fixture.


def test_a_hook_a_test_registers_is_dropped_after_it(
    auth_project: str,
    prefix: str,  # noqa: F811 - the fixture imported above.
    pytester: pytest.Pytester,
    tmp_path: Path,
) -> None:
    """The next test sees the domain's own hooks only."""
    pytester.makepyfile(
        test_hooks="""
        from domainkit.apps.hooks import on_deployment_warnings

        def test_one_adds_a_warning(client):
            on_deployment_warnings(lambda: ["only test_one's"], domain="shop")
            assert any(
                hook.call() == ["only test_one's"]
                for hook in on_deployment_warnings.of("shop")
            )

        def test_two_does_not_see_it(client):
            assert not any(
                hook.call() == ["only test_one's"]
                for hook in on_deployment_warnings.of("shop")
            )
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = run_plugin(pytester)

    result.assert_outcomes(passed=2)
