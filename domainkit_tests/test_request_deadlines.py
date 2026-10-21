"""Request deadlines over HTTP: 503 when they pass, nothing left behind."""

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.types import Message, Receive, Scope, Send

from domainkit import deadlines, setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.db.connections import INTERNAL
from domainkit.http import get_asgi_application
from domainkit.http.deadlines import DeadlineMiddleware, RequestTimeout, lift
from domainkit.testing import AsyncTestClient, assert_num_queries
from domainkit_tests import deadline_routes
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.sample_routes import payments

pytestmark = pytest.mark.anyio

ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))
postgres_only = pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
ROUTES = "domainkit_tests.deadline_routes.router"
MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.deadlines.DeadlineMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
LOGGER = "domainkit.http.deadlines"


@pytest.fixture(autouse=True)
def fresh_routes() -> Iterator[None]:
    """Start each test with the routes' records empty."""
    deadline_routes.seen.clear()
    deadline_routes.hang_payments[:] = [True]
    yield
    deadline_routes.seen.clear()


@asynccontextmanager
async def serving(**changes: Any) -> AsyncIterator[AsyncTestClient]:  # noqa: ANN401 - settings.
    """A client of the deadline routes, with ``changes`` to the settings."""
    with override_settings(ROOT_ROUTER=ROUTES, MIDDLEWARE=MIDDLEWARE, **changes):
        async with AsyncTestClient(get_asgi_application(), lifespan=False) as client:
            yield client


def warnings_from(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The deadline middleware's warnings."""
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


async def test_a_request_past_its_deadline_is_answered_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The work is cut off: 503, Retry-After, a stable code, and one warning."""
    caplog.set_level(logging.WARNING, LOGGER)
    with freeze_time("2026-01-01"):
        async with serving(REQUEST_TIMEOUT=0.05) as client:
            response = await client.get("/hang?secret=1")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    body = response.json()
    assert body == {
        "detail": "The request took too long; try again shortly.",
        "code": "deadline_exceeded",
        "request_id": response.headers["X-Request-ID"],
    }
    assert warnings_from(caplog) == ["GET /hang ran past its deadline of 0.05s"]
    # The route saw the deadline, if it ran before the deadline cut it off.
    assert deadline_routes.seen in ([], [pytest.approx(0.05)])


async def test_the_deadline_is_lifted_when_the_response_starts() -> None:
    """A streamed response has no deadline once it has started."""
    async with serving(REQUEST_TIMEOUT=30.0) as client:
        response = await client.get("/stream")

    assert response.status_code == 200
    assert response.text == "started;finished"
    assert deadline_routes.seen == [None, None]


async def test_routes_give_themselves_another_deadline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RequestTimeout lengthens, removes, or shortens the request's deadline.

    It moves the timer too (see test_deadlines), so a route given longer is
    not cut off at REQUEST_TIMEOUT.
    """
    caplog.set_level(logging.WARNING, LOGGER)
    with freeze_time("2026-01-01"):
        async with serving(REQUEST_TIMEOUT=30.0) as client:
            assert (await client.get("/longer")).json() == {"remaining": 60.0}
            assert (await client.get("/unbounded")).json() == {"remaining": None}
            shorter = await client.get("/shorter")

    assert shorter.status_code == 503
    assert warnings_from(caplog) == ["GET /shorter ran past its deadline of 0.05s"]


async def test_route_timeouts_are_checked_and_need_a_deadline() -> None:
    """Seconds above 0 and finite, or None; outside a deadline nothing moves."""
    for bad in (0, -1.0, math.inf, math.nan):
        with pytest.raises(ValueError, match="number of seconds above 0"):
            RequestTimeout(bad)
    assert RequestTimeout(None).seconds is None
    assert RequestTimeout(0.5).seconds == 0.5
    await RequestTimeout(5.0)()
    assert deadlines.remaining() is None


async def test_requests_get_request_timeout_unless_it_is_none() -> None:
    """The deadline starts with the request; None turns deadlines off."""
    with freeze_time("2026-01-01"):
        async with serving(REQUEST_TIMEOUT=12.5) as client:
            bounded = await client.get("/remaining")
        async with serving(REQUEST_TIMEOUT=None) as client:
            unbounded = await client.get("/remaining")

    assert bounded.json() == {"remaining": 12.5}
    assert unbounded.json() == {"remaining": None}


async def test_only_http_requests_get_a_deadline() -> None:
    """WebSockets and the lifespan pass through without one."""
    seen: list[tuple[str, float | None, str]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        received = await receive()  # What the server passed.
        seen.append((scope["type"], deadlines.remaining(), received["type"]))

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        """Nothing is sent."""  # pragma: no cover - nothing sends.

    with override_settings(REQUEST_TIMEOUT=5.0):
        middleware = DeadlineMiddleware(app)
    for kind in ("websocket", "lifespan"):
        await middleware({"type": kind}, receive, send)

    assert seen == [
        ("websocket", None, "lifespan.startup"),
        ("lifespan", None, "lifespan.startup"),
    ]


async def test_a_request_arriving_with_no_time_left_is_answered_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Served inside work whose deadline has passed, it never runs."""
    caplog.set_level(logging.WARNING, LOGGER)
    ran: list[str] = []
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append(scope["path"])  # pragma: no cover - never runs.

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    with override_settings(REQUEST_TIMEOUT=5.0):
        middleware = DeadlineMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": "/late", "headers": []}
    with freeze_time("2026-01-01") as clock:
        async with deadlines.timeout(1.0):
            clock.tick(1)
            await middleware(scope, receive, send)

    assert ran == []
    assert sent[0]["status"] == 503
    assert b'"code":"deadline_exceeded"' in sent[1]["body"]
    assert warnings_from(caplog) == ["GET /late ran past its deadline"]


async def test_a_deadline_found_passed_inside_is_answered_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Work that spent the time without awaiting is refused where it next waits."""
    caplog.set_level(logging.WARNING, LOGGER)
    with freeze_time("2026-01-01"):
        async with serving(REQUEST_TIMEOUT=10.0) as client:
            response = await client.get("/spent", params={"seconds": 10})

    assert response.status_code == 503
    assert response.json()["code"] == "deadline_exceeded"
    assert warnings_from(caplog) == ["GET /spent ran past its deadline of 10s"]


async def test_other_timeouts_are_errors_of_the_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TimeoutError that is not the request's deadline is a 500."""
    caplog.set_level(logging.WARNING, LOGGER)
    async with serving(REQUEST_TIMEOUT=30.0) as client:
        response = await client.get("/timeout")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert warnings_from(caplog) == []


async def test_a_response_that_started_cannot_become_a_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The error goes on to the server, which drops the connection."""
    caplog.set_level(logging.WARNING, LOGGER)
    async with serving(REQUEST_TIMEOUT=30.0) as client:
        with pytest.raises(deadlines.DeadlineExceeded):
            await client.get("/late")

    assert warnings_from(caplog) == []  # RequestContextMiddleware logs it.


async def test_a_route_that_catches_the_cancellation_answers_itself() -> None:
    """Swallowing the cancellation is the route's choice; its answer stands."""
    deadline_routes.swallowing.clear()
    deadline_routes.arrivals.value = 0
    async with serving(REQUEST_TIMEOUT=30.0) as client:
        pending = asyncio.create_task(client.get("/swallow"))
        await deadline_routes.arrivals.reach(1)  # Inside its try.
        [deadline] = deadline_routes.swallowing
        assert deadline is not None
        deadline.reset(0.0)  # The deadline passes now.
        response = await pending

    assert response.json() == {"swallowed": True}


# Cancellation leaves nothing behind --------------------------------------------


@asynccontextmanager
async def paying(
    database_url: str,
    database: dict[str, Any] | None = None,
    **changes: Any,  # noqa: ANN401 - settings.
) -> AsyncIterator[AsyncTestClient]:
    """Idempotency keys and payments on a fresh database, with a deadline.

    ``database`` adds options to its ``DATABASES`` entry.
    """
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.idempotency",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": database_url, **(database or {})}},
        ROOT_ROUTER=ROUTES,
        MIDDLEWARE=[
            *MIDDLEWARE,
            "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
        ],
        **changes,
    ):
        setup()
        async with connections.engine().begin() as connection:
            for table in (idempotency_keys, payments):
                await connection.run_sync(table.create, checkfirst=True)
        try:
            async with AsyncTestClient(
                get_asgi_application(), lifespan=False
            ) as client:
                yield client
        finally:
            await connections.close_all()
            registry.clear()


async def rows(table: Any) -> list[Any]:  # noqa: ANN401 - a Table.
    """Every row of ``table``."""
    async with connections.engine().connect() as connection:
        return list((await connection.execute(select(table))).all())


async def test_cancellation_leaves_no_transaction_key_or_connection(
    database_url: str,
) -> None:
    """A unit of work and an idempotency key, cut off mid-request.

    The payment rolls back, the key is released (a retry runs, not 409),
    and the connection goes back to the pool, outside any transaction.
    """
    key = {"Idempotency-Key": "pay-once"}
    deadline_routes.paying.clear()
    deadline_routes.arrivals.value = 0
    async with paying(database_url, REQUEST_TIMEOUT=30.0) as client:
        pending = asyncio.create_task(client.post("/slow-pay", json={}, headers=key))
        await deadline_routes.arrivals.reach(1)  # Written, not committed.
        [deadline] = deadline_routes.paying
        assert deadline is not None
        deadline.reset(0.0)  # The deadline passes now, mid-transaction.
        cut = await pending
        assert cut.status_code == 503
        assert cut.json()["code"] == "deadline_exceeded"
        assert await rows(payments) == []
        assert await rows(idempotency_keys) == []
        assert connections.pool_stats()["default"]["checked_out"] == 0
        if ON_POSTGRES:
            async with connections.engine().connect() as connection:
                stuck = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND pid <> pg_backend_pid() AND "
                        "state <> 'idle'"
                    )
                )
            assert stuck == 0

        deadline_routes.hang_payments[:] = [False]
        retried = await client.post("/slow-pay", json={}, headers=key)
        assert retried.status_code == 201
        assert len(await rows(payments)) == 1
        [stored] = await rows(idempotency_keys)
        assert stored.status_code == 201


async def test_a_short_deadline_sets_nothing_outside_postgresql(
    database_url: str,
) -> None:
    """Only PostgreSQL transactions set their statement_timeout; all of them write."""
    deadline_routes.hang_payments[:] = [False]
    with freeze_time("2026-01-01"):
        async with paying(database_url, REQUEST_TIMEOUT=5.0) as client:
            response = await client.post("/slow-pay", json={})

    assert response.status_code == 201


@postgres_only
async def test_transactions_run_with_the_time_left(database_url: str) -> None:
    """statement_timeout drops to the time left, unless that is about the same.

    A deadline within DEADLINE_SLACK of STATEMENT_TIMEOUT changes nothing,
    and sends nothing.
    """

    async def shown(
        request_timeout: float | None, statement_timeout: float | None = 30.0
    ) -> int:
        database = {"STATEMENT_TIMEOUT": statement_timeout}
        with freeze_time("2026-01-01"):
            async with paying(
                database_url, database, REQUEST_TIMEOUT=request_timeout
            ) as client:
                value = (await client.get("/statement-timeout")).json()
        return _milliseconds(value["statement_timeout"])

    assert await shown(None) == 30_000
    assert await shown(5.0) == 5_000
    assert await shown(29.0) == 29_000
    assert await shown(29.5) == 30_000  # Within DEADLINE_SLACK: left alone.
    assert await shown(5.0, statement_timeout=None) == 5_000
    assert await shown(None, statement_timeout=None) == 0


def _milliseconds(value: str) -> int:
    """``5s``, ``4999ms``, or ``0`` as milliseconds."""
    if value.endswith("ms"):
        return int(value[:-2])
    if value.endswith("s"):
        return int(value[:-1]) * 1000
    return int(value)


@postgres_only
async def test_a_lock_wait_cut_off_leaves_no_lock(database_url: str) -> None:
    """A request waiting on a row lock is answered 503 and waits no more."""
    async with paying(database_url, REQUEST_TIMEOUT=0.3) as client:
        async with connections.engine().begin() as connection:
            await connection.execute(insert(payments))  # Outside any deadline.
        holder = create_async_engine(database_url)
        try:
            async with holder.connect() as connection:
                await connection.execute(update(payments).values(id=1))  # Locks it.
                response = await client.post("/locked-pay")
                assert response.status_code == 503
                assert response.json()["code"] in {"deadline_exceeded", "database_busy"}
                waiting = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND wait_event_type = 'Lock'"
                    )
                )
                assert waiting == 0
                await connection.rollback()
        finally:
            await holder.dispose()
        assert connections.pool_stats()["default"]["checked_out"] == 0


async def test_the_frameworks_own_statements_are_not_counted() -> None:
    """assert_num_queries counts the work's statements, not INTERNAL ones."""
    with assert_num_queries(1) as queries:
        async with connections.engine().connect() as connection:
            await connection.execute(
                text("SELECT 1"), execution_options={INTERNAL: True}
            )
            await connection.execute(text("SELECT 2"))

    assert queries.statements == ["SELECT 2"]


@postgres_only
async def test_a_deadline_adds_no_query_to_count(database_url: str) -> None:
    """The statement_timeout a request's transaction sets is the framework's."""
    with freeze_time("2026-01-01"):
        async with paying(database_url, REQUEST_TIMEOUT=5.0) as client:
            with assert_num_queries(1) as queries:
                shown = (await client.get("/statement-timeout")).json()

    assert _milliseconds(shown["statement_timeout"]) == 5_000
    assert queries.statements == ["SHOW statement_timeout"]


def test_lifting_a_deadline_never_set_does_nothing() -> None:
    """A block that was never entered has no deadline to lift."""
    lift(deadlines.timeout(1.0))
