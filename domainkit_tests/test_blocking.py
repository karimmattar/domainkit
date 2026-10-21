"""Finding blocking code: slow loop steps in DEBUG and tests, sync handlers in check."""

import asyncio
import functools
import io
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.dependencies.models import Dependant
from starlette.applications import Starlette
from starlette.routing import Host

from domainkit.conf import UNSET, override_settings
from domainkit.http.application import get_asgi_application
from domainkit.http.blocking import (
    _calls,
    _listed,
    allow_sync,
    sync_calls,
    watch_event_loop,
)
from domainkit.management import call_command
from domainkit.testing import AsyncTestClient
from domainkit.testing.blocking import (
    DEBUG_VARIABLE,
    BlockingDetector,
    SlowSteps,
    configure,
    seconds,
)
from domainkit_tests.conftest import MakePackage

HERE = __name__


def held(seconds: float) -> None:
    """Hold the calling thread, and so its event loop, for ``seconds``."""
    time.sleep(seconds)


def test_slow_steps_of_a_watched_loop_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A step at or past the threshold is logged by asyncio; the rest are not."""

    async def main() -> None:
        watch_event_loop(asyncio.get_running_loop(), 0.05)
        await asyncio.sleep(0)
        held(0.2)
        await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, "asyncio"):
        asyncio.run(main())

    (slow,) = [r for r in caplog.records if r.getMessage().startswith("Executing")]
    assert slow.name == "asyncio"
    assert slow.getMessage().endswith("seconds")


timing = APIRouter()


@timing.get("/loop")
async def loop_settings() -> dict[str, object]:
    """How the serving loop is set: debug mode and its threshold."""
    loop = asyncio.get_running_loop()
    return {"debug": loop.get_debug(), "slow": loop.slow_callback_duration}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("debug", "seconds", "watched"),
    [(True, 0.5, {"debug": True, "slow": 0.5}), (True, None, None), (False, 0.5, None)],
)
async def test_debug_applications_watch_their_loop(
    debug: bool, seconds: float | None, watched: dict[str, object] | None
) -> None:
    """With DEBUG the loop logs slow steps while the application runs, then not."""
    loop = asyncio.get_running_loop()
    before = {"debug": loop.get_debug(), "slow": loop.slow_callback_duration}
    with override_settings(
        DEBUG=debug,
        SLOW_CALLBACK_DURATION=seconds,
        ROOT_ROUTER=f"{HERE}.timing",
        API_DOCS=False,
    ):
        async with AsyncTestClient(get_asgi_application()) as client:
            during = (await client.get("/loop")).json()

    assert during == (watched or before)
    assert {"debug": loop.get_debug(), "slow": loop.slow_callback_duration} == before


# Dependencies of every kind, for sync_calls.


def sync_dependency() -> int:
    """A def: FastAPI calls it on a thread."""
    return 1


async def async_dependency() -> int:
    """An async def: awaited on the loop."""
    return 1


def sync_generator() -> Iterator[int]:
    """A def that yields: entered and left on a thread."""
    yield 1


async def async_generator() -> Any:  # noqa: ANN401 - an async generator.
    """An async def that yields: on the loop."""
    yield 1


@allow_sync
def sync_on_purpose() -> int:
    """A def marked as sync on purpose."""
    return 1


def decorated[T](function: Callable[..., T]) -> Callable[..., T]:
    """A decorator that keeps the wrapped function visible."""

    @functools.wraps(function)
    def wrapper(*args: object, **kwargs: object) -> T:
        return function(*args, **kwargs)

    return wrapper


@decorated
def wrapped_sync() -> int:
    """A def behind a decorator."""
    return 1


class Built:
    """A class dependency: FastAPI builds it on a thread, and check leaves it."""


@dataclass
class SyncCallable:
    """An instance with a def ``__call__`` (and no hash: a dataclass)."""

    value: int = 1

    def __call__(self) -> int:
        """Called on a thread."""
        return self.value


class AsyncCallable:
    """An instance with an async ``__call__``."""

    async def __call__(self) -> int:
        """Awaited on the loop."""
        return 1


@allow_sync
class AllowedCallable:
    """A class marked as sync on purpose covers its instances."""

    def __call__(self) -> int:
        """Called on a thread."""
        return 1


def partial_of(value: int) -> int:
    """A def reached through ``functools.partial``."""
    return value


def build_app() -> FastAPI:
    """An application whose routes use every kind of dependency."""
    app = FastAPI()
    router = APIRouter(prefix="/included")

    @allow_sync  # Marked as the app is built, as a project's import would.
    def marked_here() -> int:
        return 1

    @app.get("/sync-handler")
    def sync_handler(
        a: Annotated[int, Depends(sync_dependency)],
        b: Annotated[int, Depends(async_dependency)],
        c: Annotated[int, Depends(sync_generator)],
        d: Annotated[int, Depends(async_generator)],
        e: Annotated[int, Depends(sync_on_purpose)],
        f: Annotated[Built, Depends(Built)],
    ) -> int:
        return a + b + c + d + e

    @app.get("/async-handler")
    async def async_handler(
        a: Annotated[int, Depends(SyncCallable())],
        b: Annotated[int, Depends(AsyncCallable())],
        c: Annotated[int, Depends(AllowedCallable())],
        d: Annotated[int, Depends(functools.partial(partial_of, 2))],
        e: Annotated[int, Depends(wrapped_sync)],
        f: Annotated[int, Depends(functools.partial(sync_on_purpose))],
        g: Annotated[int, Depends(marked_here)],
    ) -> int:
        return a + b + c + d + e + f + g

    @router.get("/reused", dependencies=[Depends(sync_dependency)])
    async def reused(a: Annotated[int, Depends(sync_dependency)]) -> int:
        return a

    @router.websocket("/socket")
    def socket(websocket: WebSocket) -> None:  # pragma: no cover - never served.
        """A sync WebSocket handler."""

    app.include_router(router)
    live = APIRouter()

    @live.websocket("/live")
    async def live_socket(websocket: WebSocket) -> None:  # pragma: no cover
        """Never served: included with a prefix and a sync dependency."""

    app.include_router(live, prefix="/v1", dependencies=[Depends(sync_generator)])
    for number in range(4):
        app.add_api_route(f"/more/{number}", sync_dependency)
    inner = FastAPI()
    inner.add_api_route("/inner", sync_dependency, methods=["POST"])
    app.mount("/mounted", inner)
    return app


def test_sync_handlers_and_dependencies_are_named_with_their_routes() -> None:
    """Each def is listed once, with the routes that use it, and how to fix it."""
    found = sync_calls(build_app(), [HERE])

    def named(call: str, routes: str) -> str:
        return (
            f"{HERE}.{call} is a def, so FastAPI calls it on its thread pool for "
            f"{routes}; make it async def, or mark it with "
            "domainkit.http.blocking.allow_sync."
        )

    assert found == [
        named("build_app.<locals>.sync_handler", "GET /sync-handler"),
        named(
            "sync_dependency",
            "GET /sync-handler, GET /included/reused, GET /more/0 and 4 more",
        ),
        named("sync_generator", "GET /sync-handler, WEBSOCKET /v1/live"),
        named("SyncCallable", "GET /async-handler"),
        named("partial_of", "GET /async-handler"),
        named("wrapped_sync", "GET /async-handler"),
        named("build_app.<locals>.socket", "WEBSOCKET /included/socket"),
    ]


def test_mounted_and_hosted_routes_are_named_by_their_whole_path() -> None:
    """Routes under a mount, a mount within it, and a host keep their full path."""
    deeper = FastAPI()
    deeper.add_api_route("/deep", sync_dependency)
    inner = FastAPI()
    inner.add_api_route("/inner", sync_dependency, methods=["POST", "GET"])
    inner.mount("/deeper", deeper)
    hosted = FastAPI()
    hosted.add_api_route("/hosted", sync_dependency)
    app = FastAPI()
    app.mount("/mounted", inner)
    app.host("api.example.com", hosted)

    assert sync_calls(app, [HERE]) == [
        (
            f"{HERE}.sync_dependency is a def, so FastAPI calls it on its thread "
            "pool for GET POST /mounted/inner, GET /mounted/deeper/deep, GET "
            "/hosted; make it async def, or mark it with "
            "domainkit.http.blocking.allow_sync."
        )
    ]


def test_hosts_and_mounts_of_included_routers_keep_their_prefix() -> None:
    """Inside an included router or a mount, routes are named with the prefix."""
    hosted = FastAPI()
    hosted.add_api_route("/hosted", sync_dependency)
    inner = APIRouter()
    inner.host("api.example.com", hosted)
    inner.mount("/mounted", hosted)
    app = FastAPI()
    app.include_router(inner, prefix="/v2")
    app.mount("/m", Starlette(routes=[Host("api.example.com", hosted)]))

    assert sync_calls(app, [HERE]) == [
        (
            f"{HERE}.sync_dependency is a def, so FastAPI calls it on its thread "
            "pool for GET /v2/hosted, GET /v2/mounted/hosted, GET /m/hosted; make "
            "it async def, or mark it with domainkit.http.blocking.allow_sync."
        )
    ]


def test_routes_past_the_third_are_counted() -> None:
    """Three routes are named; from four on, the rest are counted."""
    assert _listed(["a", "b", "c"]) == "a, b, c"
    assert _listed(["a", "b", "c", "d"]) == "a, b, c and 1 more"


def test_dependants_without_a_call_are_walked_through() -> None:
    """A dependant that calls nothing (FastAPI's own) still has its dependencies."""
    top = Dependant(dependencies=[Dependant(call=sync_dependency)])

    assert list(_calls(top)) == [sync_dependency]


def test_only_the_projects_own_code_is_named() -> None:
    """Code outside the given packages is someone else's to fix."""
    assert sync_calls(build_app(), ["elsewhere"]) == []
    assert sync_calls(build_app(), []) == []


def test_check_warns_about_sync_code_in_domains(make_package: MakePackage) -> None:
    """``check`` names the installed domains' defs, not domainkit's own."""
    make_package(
        "slowshop",
        {
            "routes.py": """
                from fastapi import APIRouter

                router = APIRouter()

                @router.get("/report")
                def report() -> dict[str, str]:
                    return {}
            """,
        },
    )
    output = io.StringIO()
    with override_settings(
        INSTALLED_DOMAINS=["slowshop", "domainkit.contrib.contenttypes"],
        ROOT_ROUTER="slowshop.routes.router",
        ALPHA=UNSET,
    ):
        call_command("check", stdout=output)

    assert output.getvalue() == (
        "System check identified no issues and 1 warning. "
        "Domains: slowshop, contenttypes.\n"
        "  Warning: slowshop.routes.report is a def, so FastAPI calls it on its "
        "thread pool for GET /report; make it async def, or mark it with "
        "domainkit.http.blocking.allow_sync.\n"
    )


BLOCKING_TESTS = """
    import time

    import pytest

    @pytest.mark.anyio
    async def test_blocks():
        time.sleep(0.25)

    @pytest.mark.anyio
    async def test_blocks_and_fails():
        time.sleep(0.25)
        assert False, "its own failure"

    @pytest.mark.anyio
    async def test_waits():
        import asyncio
        await asyncio.sleep(0.25)

    def test_sync():
        time.sleep(0.25)
"""


@pytest.fixture
def blocking_tests(pytester: pytest.Pytester) -> pytest.Pytester:
    """Tests that hold their loop, that fail, that wait, and that are sync."""
    pytester.makepyfile(test_loops=BLOCKING_TESTS)
    return pytester


def run_inner(pytester: pytest.Pytester, *options: str) -> pytest.RunResult:
    """Run the inner tests with the plugin, in order."""
    return pytester.runpytest_inprocess(
        "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov", *options
    )


def test_detect_blocking_fails_tests_that_hold_their_loop(
    blocking_tests: pytest.Pytester,
) -> None:
    """Only the test that held its loop fails for it; a failure stays its own."""
    previous = os.environ.get(DEBUG_VARIABLE)

    result = run_inner(blocking_tests, "--detect-blocking")

    assert os.environ.get(DEBUG_VARIABLE) == previous  # Put back after the run.
    result.assert_outcomes(passed=2, failed=2)
    result.stdout.fnmatch_lines(
        [
            "*_ test_blocks?asyncio? _*",
            "The test held its event loop for 0.1s or more (--detect-blocking):",
            "  Executing <Task * took *.* seconds",
            "*_ test_blocks_and_fails?asyncio? _*",
            "E       AssertionError: its own failure",
        ]
    )


def test_the_threshold_is_an_option(blocking_tests: pytest.Pytester) -> None:
    """``--blocking-seconds`` raises the bar; without the option nothing is timed."""
    raised = run_inner(blocking_tests, "--detect-blocking", "--blocking-seconds", "30")
    raised.assert_outcomes(passed=3, failed=1)  # Only its own failure.
    run_inner(blocking_tests).assert_outcomes(passed=3, failed=1)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("0.05", "0.05 is below 0.1, the least asyncio reports"),
        ("soon", "'soon' is not a number"),
    ],
)
def test_the_threshold_must_be_seconds_asyncio_reports(
    pytester: pytest.Pytester, value: str, error: str
) -> None:
    """A threshold asyncio cannot report, or not a number, is a usage error."""
    result = run_inner(pytester, "--blocking-seconds", value)

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*--blocking-seconds: {error}*"])


def test_a_quiet_asyncio_logger_still_reports_to_the_detector(
    blocking_tests: pytest.Pytester, request: pytest.FixtureRequest
) -> None:
    """With asyncio's logger kept at ERROR, tests are still timed, and it stays so."""
    asyncio_logger = logging.getLogger("asyncio")
    request.addfinalizer(
        functools.partial(asyncio_logger.setLevel, asyncio_logger.level)
    )
    asyncio_logger.setLevel(logging.ERROR)
    blocking_tests.makeconftest(
        """
        import logging

        def pytest_sessionfinish():
            assert logging.getLogger("asyncio").level == logging.ERROR
        """
    )

    result = run_inner(blocking_tests, "--detect-blocking")

    result.assert_outcomes(passed=2, failed=2)
    assert logging.getLogger("asyncio").level == logging.ERROR


def test_detection_restores_a_debug_variable_already_set(
    blocking_tests: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``PYTHONASYNCIODEBUG`` set before the run is left as it was."""
    monkeypatch.setenv(DEBUG_VARIABLE, "x")

    run_inner(blocking_tests, "--detect-blocking").assert_outcomes(passed=2, failed=2)

    assert os.environ[DEBUG_VARIABLE] == "x"


def test_the_least_threshold_is_asyncios_own() -> None:
    """``--blocking-seconds 0.1`` is allowed: it is what asyncio reports."""
    assert seconds("0.1") == 0.1


def test_a_step_exactly_at_the_threshold_counts() -> None:
    """A step that took the threshold itself is reported; a faster one is not."""
    steps = SlowSteps(0.25)
    for took in (0.25, 0.249):
        steps.emit(
            logging.LogRecord(
                "asyncio",
                logging.WARNING,
                __file__,
                1,
                "Executing %s took %.3f seconds",
                ("<Task>", took),
                None,
            )
        )

    assert steps.found == ["Executing <Task> took 0.250 seconds"]


def test_the_detector_is_registered_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--detect-blocking`` registers one named plugin, with its threshold."""
    monkeypatch.delenv(DEBUG_VARIABLE, raising=False)
    registered: list[tuple[object, str]] = []

    class Plugins:
        def register(self, plugin: object, name: str) -> None:
            registered.append((plugin, name))

    class Config:
        pluginmanager = Plugins()

        def getoption(self, name: str) -> object:
            return {"detect_blocking": True, "blocking_seconds": 0.3}[name]

    configure(Config())  # type: ignore[arg-type]

    ((detector, name),) = registered
    assert name == "domainkit-detect-blocking"
    assert isinstance(detector, BlockingDetector)
    assert detector.seconds == 0.3
    detector.pytest_unconfigure()
    assert DEBUG_VARIABLE not in os.environ


def test_unconfiguring_after_the_variable_went_is_harmless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Should something else remove ``PYTHONASYNCIODEBUG``, nothing fails."""
    monkeypatch.delenv(DEBUG_VARIABLE, raising=False)
    detector = BlockingDetector(0.1)
    del os.environ[DEBUG_VARIABLE]

    detector.pytest_unconfigure()

    assert DEBUG_VARIABLE not in os.environ
