"""Live events across processes, through Redis or PostgreSQL (user report L1).

Two hubs stand for two processes: each has a bus of its own, as each
process has, and an origin of its own. An event committed in one reaches
the subscribers of the other through the channel, and the subscribers of
its own once, not twice.
"""

import asyncio
import io
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import APIRouter, Request, WebSocket
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import text
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from domainkit.cache import caches
from domainkit.conf import override_settings, settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS, Settings
from domainkit.contrib.auth.events import UserCreated, UserPermissionGranted
from domainkit.db import connections
from domainkit.db.uow import MESSAGING
from domainkit.ddd import DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http import live_events as live_events_module
from domainkit.http.access import Public
from domainkit.http.live import live_routes, streams
from domainkit.http.live_events import LiveEvents, fanout_warnings, live_events
from domainkit.http.live_fanout import (
    NOTIFY_LIMIT,
    PostgresBackend,
    RedisBackend,
    Sender,
    backend,
)
from domainkit.http.sse import EventSourceResponse
from domainkit.management import call_command
from domainkit_tests.sample_live_routes import OwnEvents
from domainkit_tests.support.waiting import WAIT, until
from domainkit_tests.test_brokers_more import redis_url
from domainkit_tests.test_live_events import Someone, connection, granted, take

__all__ = ["redis_url"]  # The fixture the tests take.

pytestmark = pytest.mark.anyio


async def stop_listening(*hubs: LiveEvents) -> None:
    """Cancel the hubs' listeners, as shutdown cancels the loop's tasks."""
    for hub in hubs:
        for listening in list(hub._listeners.values()):
            for task in listening.values():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


def processes() -> tuple[EventBus, LiveEvents, EventBus, LiveEvents]:
    """Two processes' buses and hubs, sharing the grants of permissions."""
    here_bus, there_bus = EventBus(), EventBus()
    here, there = LiveEvents(here_bus), LiveEvents(there_bus)
    for hub in (here, there):
        hub.share(UserPermissionGranted)
    return here_bus, here, there_bus, there


async def across(
    here_bus: EventBus,
    here: LiveEvents,
    there: LiveEvents,
    listening: Callable[[], Awaitable[bool]],
) -> None:
    """The scenario both backends pass: there hears here; here hears itself once."""
    me = Someone()
    first, second = granted(me.id), granted(me.id, "shop.add_product")
    async with (
        there.subscribe(connection(me), UserPermissionGranted, rule=OwnEvents()) as far,
        here.subscribe(connection(me), UserPermissionGranted, rule=OwnEvents()) as near,
    ):
        await until(listening, "nobody listens on the channel")
        await here_bus.publish(first)
        await here_bus.publish(UserCreated(user_id=me.id, username="me"))  # Not shared.
        await here_bus.publish(second)
        async with asyncio.timeout(WAIT):
            assert await take(far, 2) == [first, second]
            assert await take(near, 2) == [first, second]  # Each once.


@pytest.fixture
def redis_fanout(redis_url: str) -> Iterator[None]:
    """``LIVE_FANOUT`` through a Redis cache of the test's own."""
    with override_settings(
        CACHES={
            "default": {"URL": "memory://"},
            "live": {"URL": redis_url, "KEY_PREFIX": "test"},
        },
        LIVE_FANOUT={"BACKEND": "redis", "ALIAS": "live"},
    ):
        yield


@pytest.mark.usefixtures("redis_fanout")
async def test_an_event_committed_in_one_process_reaches_the_others_by_redis() -> None:
    """Published on the cache's channel, heard by every subscribed process."""
    here_bus, here, _, there = processes()
    client = caches["live"].store.client  # type: ignore[attr-defined]

    async def subscribed() -> bool:
        counts = await client.pubsub_numsub(settings.LIVE_FANOUT.CHANNEL)
        return bool(counts and counts[0][1] >= 2)

    try:
        await across(here_bus, here, there, subscribed)
    finally:
        await stop_listening(here, there)
        await caches.close_all()


async def test_an_event_committed_in_one_process_reaches_the_others_by_postgres(
    database_url: str,
) -> None:
    """Notified on the database's channel, heard by every listening process."""
    if not database_url.startswith("postgresql"):
        pytest.skip("set DOMAINKIT_TEST_POSTGRES_URL")
    with override_settings(
        DATABASES={"default": {"URL": database_url}},
        LIVE_FANOUT={"BACKEND": "postgres"},
    ):
        here_bus, here, _, there = processes()

        async def listening() -> bool:
            async with connections.engine().connect() as checking:
                count = await checking.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE query LIKE 'LISTEN %domainkit_live%'"
                    )
                )
            return bool(count and count >= 2)

        try:
            await across(here_bus, here, there, listening)
        finally:
            await stop_listening(here, there)
            await connections.close_all()


# What is shared, and how ----------------------------------------------------------


def test_types_are_shared_by_name_and_one_name_is_one_type() -> None:
    """``module.QualName``: two types of one name could not be told apart."""
    hub = LiveEvents(EventBus())
    hub.share(UserPermissionGranted, UserPermissionGranted)
    assert hub.shared == (UserPermissionGranted,)

    class Impostor(DomainEvent):
        """Named as the grants are."""

    Impostor.__module__ = UserPermissionGranted.__module__
    Impostor.__qualname__ = UserPermissionGranted.__qualname__
    with pytest.raises(ImproperlyConfigured, match="Two event types are named"):
        hub.share(Impostor)


async def test_the_backend_follows_the_setting() -> None:
    """None by default; the alias must be one of its kind."""
    assert backend() is None
    with override_settings(LIVE_FANOUT={"BACKEND": "postgres"}):
        assert isinstance(backend(), PostgresBackend)
    with override_settings(
        CACHES={"default": {"URL": "memory://"}},
        LIVE_FANOUT={"BACKEND": "redis"},
    ):
        found = backend()
        assert isinstance(found, RedisBackend)
        with pytest.raises(ImproperlyConfigured, match="must be Redis"):
            found._client()
    for wrong, kind in (("postgres", "DATABASES"), ("redis", "CACHES")):
        with pytest.raises(ValidationError, match=f"not in {kind}"):
            Settings.model_validate(
                {
                    "DATABASES": {"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
                    "LIVE_FANOUT": {"BACKEND": wrong, "ALIAS": "x"},
                }
            )


async def test_an_unreadable_message_is_dropped_and_said() -> None:
    """Garbage, an unknown type, or its own: none reaches a subscriber."""
    _, here, _, there = processes()
    me = Someone()
    loop = asyncio.get_running_loop()
    async with there.subscribe(
        connection(me), UserPermissionGranted, rule=OwnEvents()
    ) as events:
        there._heard("not json", loop)
        there._heard(json.dumps({"o": "x", "t": "nowhere.Event", "d": {}}), loop)
        there._heard(json.dumps({"o": there.origin, "t": "x", "d": {}}), loop)
        event = granted(me.id)
        data = {"o": here.origin, "t": live_events_module._key(type(event))}
        there._heard(json.dumps({**data, "d": {"user_id": "?"}}), loop)
        there._heard(
            json.dumps(
                {
                    **data,
                    "d": live_events_module._adapter(type(event)).dump_python(
                        event, mode="json"
                    ),
                }
            ),
            loop,
        )
        async with asyncio.timeout(WAIT):
            assert await take(events, 1) == [event]


class Recorded:
    """A backend that keeps what it is sent, or fails."""

    def __init__(self, limit: int | None = None, *, fails: bool = False) -> None:
        """Hold messages under ``limit`` bytes; fail every send if ``fails``."""
        self.limit = limit
        self.fails = fails
        self.sent: list[list[str]] = []

    async def send(self, messages: Sequence[str]) -> None:
        """Keep ``messages``, or fail."""
        if self.fails:
            raise ConnectionError("the server went away")
        self.sent.append(list(messages))

    async def listen(self, heard: object) -> None:
        """Hear nothing."""


async def test_the_sender_keeps_to_its_limits_and_says_what_it_drops(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Too large for a notification, too many waiting, or a failed send."""
    caplog.set_level(logging.WARNING, logger="domainkit.http.live_fanout")
    small = Recorded(limit=NOTIFY_LIMIT)
    sender = Sender(small)
    sender.put("x" * (NOTIFY_LIMIT + 1), "OrderShipped")
    sender.put("fits", "OrderShipped")
    await asyncio.sleep(0.01)
    assert small.sent == [["fits"]]
    assert "A OrderShipped was not sent" in caplog.text

    monkeypatch.setattr("domainkit.http.live_fanout.MAX_WAITING", 1)
    failing = Sender(Recorded(fails=True))
    failing.put("one", "OrderShipped")
    failing.put("two", "OrderShipped")  # One waits already.
    await asyncio.sleep(0.01)
    assert "Dropped live events for the other processes: 1 already" in caplog.text
    assert "Could not send 1 live event(s)" in caplog.text


async def test_a_subscription_to_a_type_nobody_shares_is_said_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only this process's commits reach it, while the fan-out is on."""
    hub = LiveEvents(EventBus())
    monkeypatch.setattr(live_events_module, "backend", lambda: Recorded())
    monkeypatch.setattr(live_events_module, "supervise", lambda factory, name: _done())
    for _ in range(2):
        async with hub.subscribe(connection(), UserCreated, rule=OwnEvents()):
            pass

    told = [
        r for r in caplog.records if "reach the commits of this process" in r.message
    ]
    assert len(told) == 1


def _done() -> asyncio.Future[None]:
    """A listener that has already ended."""
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


def test_check_warns_about_shared_types_without_a_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``live.W003``, where deployed: the worker's events would never be seen."""
    monkeypatch.setattr(live_events, "_shared", {"x": UserPermissionGranted})
    assert fanout_warnings() == []  # Not deployed.
    monkeypatch.setattr(
        live_events_module,
        "DEPLOYED_ENVIRONMENTS",
        frozenset({settings.ENVIRONMENT}),
    )
    (warning,) = fanout_warnings()
    assert warning.startswith(
        "Domains share live events (UserPermissionGranted), and LIVE_FANOUT"
    )
    with override_settings(LIVE_FANOUT={"BACKEND": "postgres"}):
        assert fanout_warnings() == []


live_router = APIRouter()
"""Live routes and a plain one, as a project serves them."""


@live_router.websocket("/live/socket", dependencies=[Public])
async def socket(websocket: WebSocket) -> None:
    """A WebSocket: a live route."""


@live_router.get("/live/feed", dependencies=[Public])
async def feed(request: Request) -> EventSourceResponse:
    """An event stream: a live route."""
    raise NotImplementedError


@live_router.get("/plain", dependencies=[Public])
async def plain() -> dict[str, str]:
    """An answer: no live route."""
    return {}


SERVER_VARIABLES = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GRANIAN_WORKERS")


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A deployed process under uvicorn alone; the test's settings install no worker.

    The test's environment counts as deployed, as staging and production do.
    """
    monkeypatch.setattr(
        live_events_module,
        "DEPLOYED_ENVIRONMENTS",
        DEPLOYED_ENVIRONMENTS | {settings.ENVIRONMENT},
    )
    monkeypatch.setattr(sys, "argv", ["/venv/bin/uvicorn", "config.asgi:app"])
    for name in (*SERVER_VARIABLES, "GUNICORN_CMD_ARGS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_live_routes_in_several_processes_without_a_fanout_are_warned(
    deployed: pytest.MonkeyPatch,
) -> None:
    """``live.W003`` without shared types: each process sees its own commits only.

    Before, nothing warned a project that shares no type (user report L1).
    """
    routes = live_router.routes
    assert fanout_warnings(routes) == []  # One process.
    deployed.setattr(sys, "argv", ["/venv/bin/uvicorn", "app", "--workers", "4"])

    (warning,) = fanout_warnings(routes)

    assert warning.startswith(
        "Live routes (WEBSOCKET /live/socket, GET /live/feed) are served, events "
        "are committed by the server's 4 worker processes, and LIVE_FANOUT names "
        "no backend: a client sees only the events its own process committed."
    )
    assert "live_events.share" in warning
    with override_settings(LIVE_FANOUT={"BACKEND": "redis"}):
        assert fanout_warnings(routes) == []
    plain_only = [route for route in routes if getattr(route, "path", "") == "/plain"]
    assert fanout_warnings(plain_only) == []
    assert fanout_warnings() == []  # The routes are the caller's to give.


def test_the_messaging_worker_commits_in_a_process_of_its_own(
    deployed: pytest.MonkeyPatch,
) -> None:
    """One web process and the worker: the worker's events reach no client."""
    installed = SimpleNamespace(is_installed=lambda name: name == MESSAGING)
    deployed.setattr(live_events_module, "registry", installed)
    deployed.setenv("WEB_CONCURRENCY", "2")
    routes = [*live_router.routes] * 2

    (warning,) = fanout_warnings(routes)

    assert (
        "(WEBSOCKET /live/socket, GET /live/feed, WEBSOCKET /live/socket, and 1 "
        "more) are served, events are committed by the server's 2 worker "
        "processes and by the messaging worker, and LIVE_FANOUT"
    ) in warning
    deployed.setattr(live_events_module, "DEPLOYED_ENVIRONMENTS", frozenset())
    assert fanout_warnings(routes) == []  # Not deployed.


def test_a_route_is_a_stream_only_when_its_endpoint_says_so() -> None:
    """No endpoint, or an annotation that cannot be read, is not a stream."""

    async def unreadable() -> "Missing":  # type: ignore[name-defined]  # noqa: F821
        raise NotImplementedError

    routes = [route for route in live_router.routes if isinstance(route, APIRoute)]
    (feed_route,) = [route for route in routes if route.path == "/live/feed"]

    assert streams(feed_route.dependant) is True
    assert streams(None) is False
    assert streams(Dependant(call=None)) is False
    assert streams(Dependant(call=unreadable)) is False
    raw = Route("/raw", lambda request: PlainTextResponse(""))
    assert live_routes([raw, *live_router.routes]) == [
        "WEBSOCKET /live/socket",
        "GET /live/feed",
    ]


def test_failing_to_count_the_processes_is_logged_and_unknown(
    deployed: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning must not stop the application starting."""

    def broken() -> int:
        raise RuntimeError("unreadable")

    deployed.setattr(live_events_module, "server_processes", broken)

    assert fanout_warnings(live_router.routes) == []
    (record,) = [r for r in caplog.records if r.name == live_events_module.__name__]
    assert record.getMessage() == "Could not tell how many processes the server starts"


def test_check_and_a_deployed_start_warn_about_live_routes_in_several_processes(
    deployed: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``check`` reads the server's variables, and so does a deployed start."""
    deployed.setattr(sys, "argv", ["manage.py", "check"])
    deployed.setenv("GRANIAN_WORKERS", "3")
    live = {"ENVIRONMENT": "production", "ROOT_ROUTER": f"{__name__}.live_router"}

    with override_settings(**live, API_DOCS=False):
        call_command("check", stdout=(output := io.StringIO()))
        with caplog.at_level(logging.WARNING):
            get_asgi_application()

    expected = "Live routes (WEBSOCKET /live/socket, GET /live/feed) are served"
    assert f"Warning: live.W003: {expected}" in output.getvalue()
    assert any(message.startswith(expected) for message in caplog.messages)


async def test_nothing_goes_out_without_a_backend() -> None:
    """Shared, committed, and no LIVE_FANOUT: in process, as before."""
    bus = EventBus()
    hub = LiveEvents(bus)
    hub.share(UserPermissionGranted)
    await bus.publish(granted(uuid4()))
    assert hub._senders == {} or all(not each for each in hub._senders.values())
