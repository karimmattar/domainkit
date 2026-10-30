"""What the plugin puts back between tests, through public resets only (TEST-05).

Until 0.13 a project's test that subscribed a handler, registered a task,
or made a throttled log hold a line back left it for every later test, and
this repository's conftest reached into private names to undo it.
"""

import logging
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest

# Loaded before pytester takes its snapshot of the loaded modules (see
# test_testing.py): the routes define tables, Alembic keeps global state.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_auth_routes
import domainkit_tests.sample_routes  # noqa: F401
from domainkit import testing as plugin
from domainkit.conf import override_settings
from domainkit.contrib.messaging import registry as task_registry
from domainkit.contrib.messaging import task, tasks_kept
from domainkit.ddd import DomainEvent, EventBus
from domainkit.http import metrics
from domainkit.http.metrics import refreshers_kept
from domainkit.ledger import Ledger, MemoryLedger
from domainkit.ledger.handler import LedgerHandler
from domainkit.log import ThrottledLog, reset_throttled
from domainkit.money import currencies, currencies_kept, register_currency
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


def test_a_test_s_subscriptions_tasks_and_refreshers_end_with_it(
    prefix: str, pytester: pytest.Pytester
) -> None:
    """Once the domains are loaded, what a test adds is gone in the next."""
    pytester.makepyfile(
        test_kept="""
        from domainkit import setup
        from domainkit.apps import registry
        from domainkit.contrib.messaging import registry as tasks
        from domainkit.contrib.messaging import task
        from domainkit.ddd import DomainEvent, event_bus
        from domainkit.http import metrics
        from domainkit.money import currencies, register_currency

        class Happened(DomainEvent):
            pass

        async def handler(event):
            pass

        async def refresh():
            pass

        def test_loads_the_domains():
            setup()

        def test_adds():
            assert registry.ready
            event_bus.subscribe(Happened, handler)
            register_currency("PTS", minor_units=0)

            @task
            async def chore():
                pass

            metrics.register_refresher(refresh)
            assert [s for s in tasks.all_tasks() if s.key.endswith("chore")]
            assert event_bus.handlers_for(Happened) == [handler]

        def test_sees_none():
            assert event_bus.handlers_for(Happened) == []
            assert not [s for s in tasks.all_tasks() if s.key.endswith("chore")]
            assert refresh not in metrics._refreshers
            assert "PTS" not in [each.code for each in currencies()]
        """
    )

    run_plugin(pytester).assert_outcomes(passed=3)


def test_a_currency_registered_before_the_domains_load_ends_with_its_test(
    prefix: str, pytester: pytest.Pytester
) -> None:
    """A test that never loads the domains leaves no currency to the next."""
    pytester.makepyfile(
        test_currency="""
        from domainkit.apps import registry
        from domainkit.money import currencies, register_currency

        def test_registers():
            assert not registry.ready
            register_currency("PTS", minor_units=0)

        def test_sees_none():
            assert "PTS" not in [each.code for each in currencies()]
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)


def test_registrations_made_by_the_first_setup_stay(
    prefix: str, pytester: pytest.Pytester
) -> None:
    """A test whose body loads the domains keeps what their ``ready()`` registered.

    Intended: what its body registers cannot be told from what ``ready()``
    did, and dropping both would leave every later test without the
    domains' handlers. A test whose fixtures load them is kept from its
    call on (``test_sec13b_2_first_test_keeps.py``).
    """
    pytester.makepyfile(
        test_setup="""
        from domainkit import setup
        from domainkit.apps import registry
        from domainkit.ddd import DomainEvent, event_bus

        class Happened(DomainEvent):
            pass

        async def handler(event):
            pass

        def test_loads_the_domains():
            assert not registry.ready
            setup()
            event_bus.subscribe(Happened, handler)

        def test_still_subscribed():
            assert event_bus.handlers_for(Happened) == [handler]
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)


def test_throttled_lines_start_afresh_in_every_test(
    prefix: str, pytester: pytest.Pytester
) -> None:
    """A line one test's log held back is not counted against the next."""
    pytester.makepyfile(
        test_throttled="""
        import logging

        from domainkit.log import ThrottledLog

        THROTTLED = ThrottledLog(logging.getLogger("shop.security"))

        def test_first(caplog):
            THROTTLED.warning("bad_host", "10.0.0.1", "Bad host")
            assert caplog.messages == ["Bad host"]

        def test_second(caplog):
            THROTTLED.warning("bad_host", "10.0.0.1", "Bad host")
            assert caplog.messages == ["Bad host"]
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)


def test_the_principal_cache_starts_empty_in_every_test(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """A user one test signed in as is not served from the cache in the next."""
    pytester.makepyfile(
        test_principals="""
        from domainkit.contrib.auth.principals import invalidate_all

        def test_signs_in(client_for):
            assert client_for().get("/private").status_code == 200

        def test_none_cached():
            assert invalidate_all() == 0
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)


def test_modules_never_imported_are_passed_over(
    prefix: str, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reset of a module nothing imported is not called, nor the module imported."""
    never = "domainkit_tests.never_imported"
    monkeypatch.setattr(plugin, "RESETS", ((never, "reset"), *plugin.RESETS))
    monkeypatch.setattr(plugin, "KEPT", ((never, "kept"), *plugin.KEPT))
    pytester.makepyfile(
        test_loaded="""
        from domainkit import setup

        def test_loads_the_domains():
            setup()

        def test_runs():
            pass
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)
    assert never not in sys.modules


def test_each_reset_puts_back_what_the_block_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kept registrations come back, removed ones too; a reset writes nothing."""

    class Happened(DomainEvent):
        pass

    async def first(event: Happened) -> None:
        pass  # pragma: no cover - never called.

    async def second(event: Happened) -> None:
        pass  # pragma: no cover - never called.

    async def refresh() -> None:
        pass  # pragma: no cover - never called.

    bus = EventBus()
    bus.subscribe(Happened, first)
    with bus.subscriptions_kept():
        bus.unsubscribe(Happened, first)
        bus.subscribe(Happened, second)
    assert bus.handlers_for(Happened) == [first]
    with bus.subscriptions_kept():
        bus.clear()
    assert bus.handlers_for(Happened) == [first]

    before = list(task_registry.all_tasks())
    with tasks_kept():

        @task
        async def chore() -> None:
            pass  # pragma: no cover - never called.

        assert task_registry.spec_for(chore)
    assert task_registry.all_tasks() == before

    with refreshers_kept():
        metrics.register_refresher(refresh)
        assert refresh in metrics._refreshers
    assert refresh not in metrics._refreshers

    with currencies_kept():
        register_currency("PTS", minor_units=0)
    assert "PTS" not in [each.code for each in currencies()]

    throttled = ThrottledLog(logging.getLogger("shop.security"))
    throttled.warning("bad_host", "10.0.0.1", "Bad host")
    throttled.warning("bad_host", "10.0.0.1", "Bad host")
    caplog.clear()
    reset_throttled()
    throttled.flush()
    assert caplog.messages == []


def test_a_ledger_reset_forgets_memory_ledgers_only() -> None:
    """Memory ledgers start empty again; one holding connections stays open."""
    made: list[Ledger] = []

    def make(config: object) -> Ledger:
        remote = cast("Ledger", SimpleNamespace())  # Stands for immudb's.
        made.append(MemoryLedger() if not made else remote)
        return made[-1]

    handler = LedgerHandler({"memory": make})
    with override_settings(
        LEDGERS={"evidence": {"URL": "memory://"}, "remote": {"URL": "memory://"}}
    ):
        memory, remote = handler["evidence"], handler["remote"]
        handler.reset()
        assert handler["remote"] is remote
        assert handler["evidence"] is not memory
