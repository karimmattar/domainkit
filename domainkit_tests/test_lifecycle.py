"""Shutting down in phases, within one budget: every resource, even when one fails."""

import asyncio
import importlib
import logging
from collections.abc import Iterator
from functools import partial
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from domainkit import lifecycle
from domainkit.apps import DomainRegistry
from domainkit.broker import brokers
from domainkit.broker.handler import BrokerHandler
from domainkit.cache import CacheHandler, DummyStore
from domainkit.cache import caches as cache_handler
from domainkit.conf import override_settings
from domainkit.conf.global_settings import Settings
from domainkit.db.connections import ConnectionHandler, connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.ledger import ledgers
from domainkit.lifecycle import (
    PHASE_ENDS,
    Budget,
    Phase,
    close_resources,
    register,
    serving,
    shutdown_timeout,
    unregister,
)
from domainkit.management.base import run_async
from domainkit.tasks import owned, spawn
from domainkit.utils import closing as closing_module
from domainkit.utils.closing import (
    LEFT_BEHIND,
    RESERVED_SHARE,
    close_each,
    close_in_turn,
    forget_outcome,
)
from domainkit_tests.conftest import ALPHA, MakePackage
from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def registered_closers() -> Iterator[None]:
    """Forget the closers a test registers."""
    saved = {phase: dict(closers) for phase, closers in lifecycle._registered.items()}
    yield
    for phase, closers in lifecycle._registered.items():
        closers.clear()
        closers.update(saved[phase])


def record_closes(monkeypatch: pytest.MonkeyPatch, closed: list[str]) -> None:
    """Make the framework's own kinds record that they closed, by name."""
    handlers = {
        "caches": cache_handler,
        "databases": connections,
        "ledgers": ledgers,
        "brokers": brokers,
    }
    for name, handler in handlers.items():

        async def close_all(name: str = name) -> None:
            closed.append(name)

        monkeypatch.setattr(handler, "close_all", close_all)


async def hang_until_cancelled(closed: list[str], name: str) -> None:
    """Record ``name``, then wait until cancelled."""
    closed.append(name)
    await asyncio.Event().wait()


def spent_through(phase: Phase, seconds: float = 100.0) -> Budget:
    """A budget whose ``phase`` (and those before it) ended just now.

    Closers of those phases are cut off at once, after their first step:
    one that hangs is cut off whatever the machine's speed. The later
    phases keep their whole share, tens of seconds, which no closer here
    comes near; so no test depends on how fast the host runs.
    """
    loop = asyncio.get_running_loop()
    return Budget(loop.time() - seconds * PHASE_ENDS[phase], seconds)


async def test_every_resource_closes_even_when_one_fails_or_hangs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Kinds close at once: a failing or hung one is logged; the rest close."""
    closed: list[str] = []

    async def fails(name: str) -> None:
        closed.append(name)
        raise RuntimeError(f"{name} gone")

    monkeypatch.setattr(cache_handler, "close_all", partial(fails, "caches"))
    monkeypatch.setattr(
        connections, "close_all", partial(hang_until_cancelled, closed, "databases")
    )
    monkeypatch.setattr(ledgers, "close_all", partial(fails, "ledgers"))
    monkeypatch.setattr(brokers, "close_all", partial(fails, "brokers"))

    async def flushed() -> None:
        """The flush phase is not under test: it may need a thread on a busy host."""

    monkeypatch.setattr(lifecycle, "aflush_reports", flushed)
    monkeypatch.setattr(lifecycle, "aflush_logs", flushed)
    monkeypatch.setattr(lifecycle, "close_http_clients", flushed)

    with caplog.at_level(logging.ERROR, "domainkit.lifecycle"):
        async with asyncio.timeout(5):  # Its share is over: the hang is cut at once.
            await close_resources(spent_through(Phase.RESOURCES))

    assert closed == ["caches", "databases", "ledgers", "brokers"]
    assert sorted(caplog.messages) == [
        "Could not close the brokers; closing the rest",
        "Could not close the caches; closing the rest",
        "Could not close the databases within the shutdown budget; closing the rest",
        "Could not close the ledgers; closing the rest",
    ]
    causes = {
        r.getMessage().split()[4].rstrip(";"): r.exc_info[1] if r.exc_info else None
        for r in caplog.records
    }
    assert causes["databases"] is None
    assert {name: str(causes[name]) for name in ("caches", "ledgers", "brokers")} == {
        "caches": "caches gone",
        "ledgers": "ledgers gone",
        "brokers": "brokers gone",
    }


async def test_phases_run_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Work in flight drains, tasks stop, resources close, then logs flush."""
    closed: list[str] = []
    record_closes(monkeypatch, closed)

    async def note(name: str) -> None:
        closed.append(name)

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            closed.append("task")

    register("http clients", partial(note, "http clients"))
    register("publishers", partial(note, "publishers"), phase=Phase.DRAIN)
    register("exports", partial(note, "exports"), phase=Phase.FLUSH)
    spawn(background(), name="test.background")
    await asyncio.sleep(0)

    await close_resources()

    assert closed == [
        "publishers",
        "task",
        "caches",
        "databases",
        "ledgers",
        "brokers",
        "http clients",
        "exports",
    ]


async def test_error_reports_then_logs_are_flushed_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework flushes reports, then logs, after the flush phase's closers.

    What closing everything else reported or logged is then written too.
    """
    closed: list[str] = []
    record_closes(monkeypatch, closed)

    async def note(name: str) -> None:
        closed.append(name)

    monkeypatch.setattr(lifecycle, "aflush_reports", partial(note, "error reports"))
    monkeypatch.setattr(lifecycle, "aflush_logs", partial(note, "logs"))
    register("exports", partial(note, "exports"), phase=Phase.FLUSH)

    await close_resources()

    assert closed[-3:] == ["exports", "error reports", "logs"]
    for name in ("error reports", "logs"):
        with pytest.raises(ValueError, match="closed by the framework already"):
            register(name, partial(note, name), phase=Phase.FLUSH)


async def test_a_hung_phase_leaves_the_later_phases_their_share(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A drain that hangs is cut off at its share; later phases still close."""
    closed: list[str] = []
    record_closes(monkeypatch, closed)
    release = asyncio.Event()

    async def ignores_cancellation() -> None:
        closed.append("stubborn")
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    async def note(name: str) -> None:
        closed.append(name)

    register("stubborn", ignores_cancellation, phase=Phase.DRAIN)
    register("exports", partial(note, "exports"), phase=Phase.FLUSH)

    try:
        with caplog.at_level(logging.ERROR, "domainkit.lifecycle"):
            async with asyncio.timeout(5):
                # The drain's and the tasks' shares are over; the others are not.
                await close_resources(spent_through(Phase.TASKS))
    finally:
        release.set()  # Fail fast, not hang, if anything above goes wrong.

    assert closed == [
        "stubborn",
        "caches",
        "databases",
        "ledgers",
        "brokers",
        "exports",
    ]
    assert caplog.messages == [
        "Could not close the stubborn within the shutdown budget; closing the rest",
        # Left behind, owned: the tasks phase waited for it, to its end.
        (
            "Background tasks still running at the end of their shutdown phase: "
            "domainkit.close:stubborn"
        ),
    ]


async def test_a_close_cut_off_ends_before_the_resources_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain cut off at its share still ends before what it uses is closed.

    It is cancelled and left behind as an owned task, so the tasks phase
    waits for it to end, then the resources phase closes the databases.
    """
    closed: list[str] = []
    record_closes(monkeypatch, closed)

    async def slow_to_stop() -> None:
        closed.append("draining")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            for _ in range(20):  # Writing its last rows, after being cut off.
                await asyncio.sleep(0)
            closed.append("drained")
            raise

    register("publishers", slow_to_stop, phase=Phase.DRAIN)

    async with asyncio.timeout(30):
        await close_resources(spent_through(Phase.DRAIN))

    assert closed[:3] == ["draining", "drained", "caches"]


async def test_tasks_that_will_not_stop_are_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The tasks phase ends at its share even if a task ignores cancellation."""
    record_closes(monkeypatch, [])

    async def flushed() -> None:
        """The flush phase is not under test: it may need a thread on a busy host."""

    monkeypatch.setattr(lifecycle, "aflush_reports", flushed)
    monkeypatch.setattr(lifecycle, "aflush_logs", flushed)
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    stuck = [spawn(stubborn(), name=f"test.stubborn{n}") for n in (1, 2)]
    await asyncio.sleep(0)
    try:
        with caplog.at_level(logging.ERROR, "domainkit.lifecycle"):
            async with asyncio.timeout(5):  # The tasks' share is over at once.
                await close_resources(spent_through(Phase.TASKS))
    finally:
        release.set()
        await asyncio.wait(stuck, timeout=5)

    assert caplog.messages == [
        (
            "Background tasks still running at the end of their shutdown phase: "
            "test.stubborn1, test.stubborn2"
        )
    ]


def test_each_phase_ends_at_its_share_of_the_budget() -> None:
    """Shares grow phase by phase and end with the budget."""
    budget = Budget(started=100.0, seconds=10.0)

    assert [budget.end(phase) for phase in Phase] == [103.0, 105.0, 106.0, 109.0, 110.0]
    assert list(PHASE_ENDS) == list(Phase)


async def test_the_budget_is_shutdown_timeout() -> None:
    """``SHUTDOWN_TIMEOUT``, or its default where no settings load."""
    loop = asyncio.get_running_loop()
    with override_settings(SHUTDOWN_TIMEOUT=3.5):
        assert shutdown_timeout() == 3.5
        budget = Budget.begin()
    assert budget.seconds == 3.5
    assert budget.started <= loop.time()
    assert Budget.begin(1.0).seconds == 1.0
    assert shutdown_timeout() == 15.0


def test_without_settings_the_budget_is_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands that load no settings still shut down within a budget."""

    class Unloaded:
        def __getattr__(self, name: str) -> object:
            raise ImproperlyConfigured("No settings module.")

    monkeypatch.setattr(lifecycle, "settings", Unloaded())
    assert shutdown_timeout() == lifecycle.DEFAULT_SHUTDOWN_TIMEOUT == 15.0


@pytest.mark.parametrize("seconds", [0, -1, 301])
def test_shutdown_timeout_is_bounded(seconds: float) -> None:
    """A budget of nothing, or of more than five minutes, is refused."""
    with pytest.raises(ValidationError, match="SHUTDOWN_TIMEOUT"):
        Settings(SHUTDOWN_TIMEOUT=seconds)


def test_register_refuses_the_wrong_phase_or_a_framework_kind() -> None:
    """Only drain, resources, and flush take closers; built-in kinds are taken."""

    async def close() -> None:
        return None

    for phase in (Phase.DOMAINS, Phase.TASKS):
        with pytest.raises(ValueError, match="drain, resources, or flush"):
            register("mine", close, phase=phase)
    with pytest.raises(ValueError, match="'databases' is closed by the framework"):
        register("databases", close)
    unregister("never registered")


async def test_registering_a_name_again_replaces_its_closer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One closer per name, in the phase it was last registered for."""
    closed: list[str] = []
    record_closes(monkeypatch, closed)

    async def note(name: str) -> None:
        closed.append(name)

    register("clients", partial(note, "first"))
    register("clients", partial(note, "second"), phase=Phase.FLUSH)
    await close_resources()
    assert closed[-1] == "second"
    assert "first" not in closed

    unregister("clients")
    closed.clear()
    await close_resources()
    assert "second" not in closed


def test_management_commands_run_every_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_async`` drains, stops tasks, and closes, as the application does."""
    closed: list[str] = []
    record_closes(monkeypatch, closed)

    async def note(name: str) -> None:
        closed.append(name)

    async def command() -> None:
        spawn(asyncio.Event().wait(), name="test.command")

    register("publishers", partial(note, "publishers"), phase=Phase.DRAIN)
    run_async(command())
    assert closed == ["publishers", "caches", "databases", "ledgers", "brokers"]


def test_the_application_shuts_down_domains_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain hooks, then the phases of ``close_resources``."""
    closed: list[str] = []
    record_closes(monkeypatch, closed)
    monkeypatch.setattr(
        AlphaConfig, "shutdown", lambda self: _note(closed, "alpha shutdown")
    )

    with TestClient(get_asgi_application()):
        pass

    assert closed == ["alpha shutdown", "caches", "databases", "ledgers", "brokers"]


async def _note(closed: list[str], name: str) -> None:
    """Record ``name``."""
    closed.append(name)


async def test_a_hung_domain_is_cut_off_and_the_rest_still_stop(
    make_package: MakePackage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hung domain's error surfaces after every domain and resource closed."""
    make_package(
        "hung",
        {
            "apps.py": """
                import asyncio

                from domainkit.apps import DomainConfig
                from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

                class HungConfig(DomainConfig):
                    async def shutdown(self) -> None:
                        AlphaConfig.calls.append("hung shutdown")
                        await asyncio.Event().wait()
            """
        },
    )
    domains = DomainRegistry()
    domains.populate([ALPHA, "hung"])
    monkeypatch.setattr(lifecycle, "registry", domains)
    closed: list[str] = []
    record_closes(monkeypatch, closed)

    with (
        override_settings(SHUTDOWN_TIMEOUT=0.05),
        pytest.raises(TimeoutError, match=r"<HungConfig: hung> did not close within"),
    ):
        async with asyncio.timeout(5), serving():
            pass

    assert AlphaConfig.calls == ["ready", "startup", "hung shutdown", "shutdown"]
    assert closed == ["caches", "databases", "ledgers", "brokers"]


async def test_a_domain_that_fails_to_start_still_shuts_everything_down(
    make_package: MakePackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Started domains stop and resources close; the startup error is raised."""
    make_package(
        "broken",
        {
            "apps.py": """
                from domainkit.apps import DomainConfig

                class BrokenConfig(DomainConfig):
                    async def startup(self) -> None:
                        raise RuntimeError("cannot start")
            """
        },
    )
    domains = DomainRegistry()
    domains.populate([ALPHA, "broken"])
    monkeypatch.setattr(lifecycle, "registry", domains)
    closed: list[str] = []
    record_closes(monkeypatch, closed)

    with pytest.raises(RuntimeError, match="cannot start"):
        async with serving():
            pytest.fail("The body runs only once every domain started.")

    assert AlphaConfig.calls == ["ready", "startup", "shutdown"]
    assert closed == ["caches", "databases", "ledgers", "brokers"]


def test_closing_what_a_loop_never_opened_does_nothing() -> None:
    """Handlers close only the running loop's resources, if it has any."""

    async def close_fresh() -> None:
        await CacheHandler().close_all()
        await ConnectionHandler().close_all()
        await BrokerHandler().close_all()

    asyncio.run(close_fresh())


async def test_close_each_closes_every_item_then_raises_the_first_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Later failures are logged, since only the first can be raised."""
    closed: list[str] = []

    async def close(item: str) -> None:
        closed.append(item)
        if item != "c":
            raise RuntimeError(f"{item} failed")

    with (
        caplog.at_level(logging.ERROR, "domainkit.utils.closing"),
        pytest.raises(RuntimeError, match=r"^a failed$"),
    ):
        await close_each(["a", "b", "c"], close)

    assert closed == ["a", "b", "c"]
    assert caplog.messages == ["Could not close 'b'"]
    cause = caplog.records[0].exc_info
    assert cause
    assert str(cause[1]) == "b failed"
    await close_each(["c"], close)  # Nothing failed: nothing raised.
    await close_each([], close)
    assert closed[-1] == "c"


async def test_close_each_closes_the_others_while_one_hangs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung item holds up no one; when the wait gives up, it is named."""
    closed: list[str] = []

    async def close(item: str) -> None:
        if item == "hung":
            await asyncio.Event().wait()
        if item == "fails":
            raise RuntimeError("fails")
        closed.append(item)

    with (
        caplog.at_level(logging.ERROR, "domainkit.utils.closing"),
        pytest.raises(TimeoutError),
    ):
        async with asyncio.timeout(0.02):
            await close_each(["first", "fails", "hung", "last"], close)

    assert closed == ["first", "last"]
    assert caplog.messages == ["Could not close 'fails'", "Gave up closing 'hung'"]
    cause = caplog.records[0].exc_info
    assert cause
    assert str(cause[1]) == "fails"


async def test_closes_given_up_on_end_without_an_unretrieved_error() -> None:
    """A failure or cancellation after the wait gave up is retrieved quietly."""
    loop = asyncio.get_running_loop()
    failed, cancelled = loop.create_future(), loop.create_future()
    failed.set_exception(RuntimeError("too late"))
    cancelled.cancel()

    forget_outcome(failed)
    forget_outcome(cancelled)

    assert failed._log_traceback is False


async def test_close_each_raises_a_close_cancelled_on_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A close cancelled by something else is a failure too, and hides no other."""

    async def close(item: str) -> None:
        if item == "cancelled":
            raise asyncio.CancelledError
        if item == "fails":
            raise RuntimeError("fails")

    with (
        caplog.at_level(logging.ERROR, "domainkit.utils.closing"),
        pytest.raises(asyncio.CancelledError),
    ):
        await close_each(["fine", "cancelled", "fails"], close)
    assert caplog.messages == ["Could not close 'fails'"]


async def test_failures_after_a_close_that_worked_are_raised() -> None:
    """Items that closed are passed over, not the end of the search."""

    async def close(item: str) -> None:
        if item == "fails":
            raise RuntimeError("fails after one that closed")

    with pytest.raises(RuntimeError, match="after one that closed"):
        await close_each(["fine", "fails"], close)
    with pytest.raises(RuntimeError, match="after one that closed"):
        await close_in_turn(["fine", "fails"], close)


class Turns:
    """``asyncio`` as ``close_in_turn`` sees it, on a clock only its waits move.

    A wait gives the item a few turns of the event loop and ends, with no
    time passed if the item has closed by then. An item still open has used
    its whole share: the clock moves on by it, and the item is cut off, for
    real. So the shares are exact, however slow the machine, and the tests
    take no time.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stand in for ``asyncio`` in ``domainkit.utils.closing``."""
        self.now = 0.0
        self.shares: list[float | None] = []
        monkeypatch.setattr(closing_module, "asyncio", self)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - asyncio's names.
        """Everything else is asyncio's own."""
        return getattr(asyncio, name)

    def get_running_loop(self) -> "Turns":
        """The loop whose clock ``close_in_turn`` reads: this one."""
        return self

    def time(self) -> float:
        """The clock, in seconds."""
        return self.now

    async def wait(
        self,
        futures: list["asyncio.Future[Any]"],
        *,
        timeout: float | None,  # noqa: ASYNC109 - asyncio.wait's signature.
    ) -> tuple[set["asyncio.Future[Any]"], set["asyncio.Future[Any]"]]:
        """Note the share; return what closed, using all of it if not."""
        self.shares.append(timeout)
        if timeout is None:  # No limit: as long as it takes.
            return await asyncio.wait(futures)
        for _ in range(5):
            await asyncio.sleep(0)
        done = {future for future in futures if future.done()}
        pending = set(futures) - done
        if pending:
            self.now += timeout
        return done, pending


async def test_close_in_turn_shares_the_time_and_tries_every_item(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One that hangs is cut off at its share; the next still gets its own."""
    turns = Turns(monkeypatch)
    closed: list[str] = []

    async def close(item: str) -> None:
        closed.append(item)
        if item == "hung":
            await asyncio.Event().wait()
        if item == "fails":
            raise RuntimeError("fails")

    with (
        caplog.at_level(logging.ERROR, "domainkit.utils.closing"),
        pytest.raises(TimeoutError, match=r"^'hung' did not close within 0\.50s$"),
    ):
        async with asyncio.timeout(5):
            await close_in_turn(["hung", "fails", "last"], close, within=0.6)
    assert closed == ["hung", "fails", "last"]
    # Each keeps a reserve of 0.05 s for every item after it.
    assert turns.shares == pytest.approx([0.5, 0.05, 0.1])
    assert caplog.messages == ["Could not close 'fails'"]
    cause = caplog.records[0].exc_info
    assert cause
    assert type(cause[1]) is RuntimeError  # Only overruns become TimeoutErrors.

    closed.clear()
    await close_in_turn(["a", "b"], close)  # No limit: in order.
    assert closed == ["a", "b"]
    assert turns.shares[3:] == [None, None]


async def test_close_in_turn_lets_one_item_use_most_of_the_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Less only the reserves of the items after it, not an equal split."""
    turns = Turns(monkeypatch)
    closed: list[str] = []

    async def close(item: str) -> None:
        if item == "slow":
            await asyncio.Event().wait()
        closed.append(item)

    # Two items: an equal split would cut "slow" at 0.20 s.
    with pytest.raises(TimeoutError, match=r"^'slow' did not close within 0\.35s$"):
        async with asyncio.timeout(5):
            await close_in_turn(["slow", "quick"], close, within=0.4)
    assert closed == ["quick"]
    assert turns.shares == pytest.approx([0.35, 0.05])
    assert RESERVED_SHARE == 0.25
    await close_in_turn([], close, within=1)  # Nothing to close.


async def test_close_in_turn_keeps_only_the_later_items_reserves(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last item may use all that is left: no reserve is kept after it."""
    turns = Turns(monkeypatch)

    async def hangs(item: str) -> None:
        await asyncio.Event().wait()

    with (
        caplog.at_level(logging.ERROR, "domainkit.utils.closing"),
        pytest.raises(TimeoutError, match=r"^'first' did not close"),
    ):
        async with asyncio.timeout(5):
            await close_in_turn(["first", "last"], hangs, within=0.4)

    cause = caplog.records[0].exc_info
    assert cause
    # "first" used 0.35 s of 0.4, keeping 0.05 for "last"; "last" keeps none.
    assert str(cause[1]) == "'last' did not close within 0.05s"
    assert turns.shares == pytest.approx([0.35, 0.05])


class Stubborn:
    """A close that ignores the first cancellation, then waits to be released.

    As a flush behind ``asyncio.shield`` would. A second cancellation (the
    test's own deadline) stops it, so a regression fails instead of hanging.
    """

    def __init__(self) -> None:
        """Hold no task yet."""
        self.release = asyncio.Event()
        self.held = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []

    async def hold(self) -> None:
        """Wait until released, whatever cancels it meanwhile.

        Released after a cancellation it ignored, it fails: it closed late.
        """
        self.tasks.append(cast("asyncio.Task[Any]", asyncio.current_task()))
        self.held.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.release.wait()
            raise RuntimeError("closed too late") from None

    async def let_go(self) -> None:
        """Release it, and wait for the tasks it held to end."""
        self.release.set()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=5)


async def test_close_in_turn_leaves_an_item_that_ignores_cancellation_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cut off at its share and cancelled, it holds up none of the later items."""
    Turns(monkeypatch)
    stubborn = Stubborn()
    closed: list[str] = []

    async def close(item: str) -> None:
        closed.append(item)
        if item == "stubborn":
            await stubborn.hold()

    try:
        with pytest.raises(
            TimeoutError, match=r"^'stubborn' did not close within 0\.35s$"
        ):
            async with asyncio.timeout(5):
                await close_in_turn(["stubborn", "last"], close, within=0.4)
        assert closed == ["stubborn", "last"]
        [held] = stubborn.tasks
        assert not held.done()  # Left behind, still running.
        assert held.cancelling()
        assert held in owned()  # The shutdown's tasks phase waits for it.
        assert held.get_name() == LEFT_BEHIND
    finally:
        await stubborn.let_go()
    assert held._log_traceback is False  # Its failure is not logged...
    assert isinstance(held.exception(), RuntimeError)  # ...but it did fail.


async def test_close_in_turn_gives_up_the_close_running_when_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The close in progress is cancelled too; later items are not started."""
    stubborn = Stubborn()
    closed: list[str] = []

    async def close(item: str) -> None:
        closed.append(item)
        await stubborn.hold()

    closing = asyncio.create_task(close_in_turn(["first", "second"], close))
    try:
        async with asyncio.timeout(5):
            await stubborn.held.wait()  # Closing "first".
        with caplog.at_level(logging.ERROR, "domainkit.utils.closing"):
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                async with asyncio.timeout(5):
                    await closing
        assert closed == ["first"]
        assert caplog.messages == ["Gave up closing 'first'"]
        [held] = stubborn.tasks
        assert held.cancelling()
    finally:
        await stubborn.let_go()


async def test_close_in_turn_raises_a_close_that_cancelled_itself_last() -> None:
    """As ``close_each`` does: the later items still close first."""
    closed: list[str] = []

    async def close(item: str) -> None:
        closed.append(item)
        if item == "cancelled":
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await close_in_turn(["cancelled", "last"], close, within=5)
    assert closed == ["cancelled", "last"]


async def test_a_domain_that_ignores_cancellation_cannot_hold_shutdown(
    make_package: MakePackage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its hook is cut off at its share; the domains and phases after it run."""
    make_package(
        "stubborn",
        {
            "apps.py": """
                import asyncio

                from domainkit.apps import DomainConfig
                from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

                RELEASE = asyncio.Event()
                TASKS = []

                class StubbornConfig(DomainConfig):
                    async def shutdown(self) -> None:
                        AlphaConfig.calls.append("stubborn shutdown")
                        TASKS.append(asyncio.current_task())
                        try:
                            await RELEASE.wait()
                        except asyncio.CancelledError:  # Ignored, once.
                            await RELEASE.wait()
            """
        },
    )
    domains = DomainRegistry()
    domains.populate([ALPHA, "stubborn"])
    monkeypatch.setattr(lifecycle, "registry", domains)
    closed: list[str] = []
    record_closes(monkeypatch, closed)
    hook = importlib.import_module("stubborn.apps")

    try:
        with (
            override_settings(SHUTDOWN_TIMEOUT=0.05),
            pytest.raises(
                TimeoutError, match=r"<StubbornConfig: stubborn> did not close within"
            ),
        ):
            async with asyncio.timeout(5), serving():
                pass
        assert AlphaConfig.calls == [
            "ready",
            "startup",
            "stubborn shutdown",
            "shutdown",
        ]
        assert closed == ["caches", "databases", "ledgers", "brokers"]
    finally:
        hook.RELEASE.set()
        await asyncio.wait(hook.TASKS, timeout=5)


async def test_the_domain_lifespan_without_a_limit_waits_for_hooks() -> None:
    """``stop_within`` defaults to no limit, as before."""
    domains = DomainRegistry()
    domains.populate([ALPHA])
    async with domains.lifespan():
        pass
    assert AlphaConfig.calls == ["ready", "startup", "shutdown"]


class BrokenStore(DummyStore):
    """A store whose close fails, after recording it."""

    def __init__(self, closed: list[str], name: str, *, fails: bool) -> None:
        """Report closing as ``name``; fail if ``fails``."""
        self.closed = closed
        self.name = name
        self.fails = fails

    async def close(self) -> None:
        """Record the close, then fail if asked to."""
        self.closed.append(self.name)
        if self.fails:
            raise RuntimeError(f"{self.name} would not close")


def test_handlers_close_every_resource_when_one_fails() -> None:
    """A cache that fails to close does not keep the next one open."""
    closed: list[str] = []
    handler = CacheHandler(
        connect=lambda config: BrokenStore(
            closed, config.KEY_PREFIX or "", fails=config.KEY_PREFIX == "one"
        )
    )
    caches = {
        "default": {},
        "one": {"URL": "redis://cache:6379/0", "KEY_PREFIX": "one"},
        "two": {"URL": "redis://cache:6379/0", "KEY_PREFIX": "two"},
    }

    async def use_then_close() -> None:
        handler["one"], handler["two"]
        await handler.close_all()

    with (
        override_settings(CACHES=caches),
        pytest.raises(RuntimeError, match=r"^one would not close$"),
    ):
        asyncio.run(use_then_close())

    assert closed == ["one", "two"]
