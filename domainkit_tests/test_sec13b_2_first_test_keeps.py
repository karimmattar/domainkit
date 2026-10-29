"""SEC-13b-2: the run's first client test keeps its subscriptions and tasks.

The plugin puts the event bus, the tasks, and the refreshers back after a
test only if the domains were loaded when the test started. The first test
that requests ``client`` (or ``db``, ``live_server``) loads them in its own
setup, so a handler it subscribes, or a task it registers, in its body
stays for every later test: they deliver its events to it, in whatever
order the run happens to take.
"""

from pathlib import Path

import pytest

# Loaded before pytester takes its snapshot of the loaded modules (see
# test_testing.py): the routes define tables, Alembic keeps global state.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_routes  # noqa: F401
from domainkit.conf import override_settings
from domainkit.testing import IN_MEMORY
from domainkit_tests.test_testing import prefix, run_plugin

__all__ = ["prefix"]


def test_the_first_client_test_s_handler_ends_with_it(
    prefix: str, pytester: pytest.Pytester, tmp_path: Path
) -> None:
    """What the body of the test that loaded the domains subscribed is gone after it."""
    pytester.makepyfile(
        test_first="""
        from domainkit.contrib.messaging import registry as tasks
        from domainkit.contrib.messaging import task
        from domainkit.ddd import DomainEvent, event_bus

        class Happened(DomainEvent):
            pass

        SEEN = []

        async def handler(event):
            SEEN.append(event)

        def test_first_with_a_client(client):
            event_bus.subscribe(Happened, handler)

            @task
            async def chore():
                pass

        def test_sees_none():
            assert event_bus.handlers_for(Happened) == []
            assert not [s for s in tasks.all_tasks() if s.key.endswith("chore")]
        """
    )

    (tmp_path / "migrations").mkdir()
    with override_settings(
        MIGRATIONS_DIR=tmp_path / "migrations",
        DATABASES={"default": {"URL": IN_MEMORY}},
    ):
        result = run_plugin(pytester, "-o", "domainkit_settings=project.settings.test")
    result.assert_outcomes(passed=2)
