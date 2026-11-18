"""Request deadlines over HTTP: 503 when they pass, nothing left behind."""

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Final

import anyio
import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.util import await_only
from starlette.types import Message, Receive, Scope, Send

from domainkit import deadlines, setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.db import uow as uow_module
from domainkit.db.connections import INTERNAL, SETTLE_TIMEOUT
from domainkit.http import get_asgi_application
from domainkit.http.deadlines import DeadlineMiddleware, RequestTimeout, lift
from domainkit.testing import AsyncTestClient, assert_num_queries
from domainkit_tests import deadline_routes
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.sample_routes import PaymentsUnitOfWork, payments

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
BOUNDED: Final = pytest.mark.timeout(30, method="signal")
"""For the tests of work cut off: each is bounded, so one that regresses
fails rather than hangs."""


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
    # Never routed, so no template: logs never show the path as sent.
    assert warnings_from(caplog) == ["GET unmatched ran past its deadline"]


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
    middleware: tuple[str, ...] = (),
    **changes: Any,  # noqa: ANN401 - settings.
) -> AsyncIterator[AsyncTestClient]:
    """Idempotency keys and payments on a fresh database, with a deadline.

    ``database`` adds options to its ``DATABASES`` entry; ``middleware``
    goes innermost.
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
            *middleware,
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
    # The caller is named (X-API-Key): since 0.11 an anonymous key is refused.
    key = {"Idempotency-Key": "pay-once", "X-API-Key": "payer"}
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
            # The cut-off connection settles in the background, within
            # SETTLE_TIMEOUT: until then its server process may still be busy.
            for _ in range(int((SETTLE_TIMEOUT + 1) / 0.1)):
                async with connections.engine().connect() as connection:
                    stuck = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                            "current_database() AND pid <> pg_backend_pid() AND "
                            "state <> 'idle'"
                        )
                    )
                if stuck == 0:
                    break
                await asyncio.sleep(0.1)
            assert stuck == 0

        deadline_routes.hang_payments[:] = [False]
        retried = await client.post("/slow-pay", json={}, headers=key)
        assert retried.status_code == 201
        assert len(await rows(payments)) == 1
        [stored] = await rows(idempotency_keys)
        assert stored.status_code == 201


async def cut_off(client: AsyncTestClient, path: str) -> tuple[Any, float]:
    """POST ``path``, which writes and waits, and pass its deadline once it waits.

    Returns the answer, and the seconds it came after the deadline passed.
    A request that never answers fails the test.
    """
    deadline_routes.paying.clear()
    deadline_routes.arrivals.value = 0
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(10):
        pending = asyncio.create_task(client.post(path, json={}))
        await deadline_routes.arrivals.reach(1)  # Written, not committed.
        [deadline] = deadline_routes.paying
        assert deadline is not None
        deadline.reset(0.0)  # The deadline passes now, mid-transaction.
        passed = loop.time()
        answer = await pending
    return answer, loop.time() - passed


def checked_out() -> int:
    """The connections of the default pool not given back."""
    return connections.pool_stats()["default"]["checked_out"]


async def busy_sessions() -> int:
    """The database's other sessions that are not idle: in a transaction, say."""
    async with connections.engine().connect() as connection:
        busy = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                "current_database() AND pid <> pg_backend_pid() AND state <> 'idle'"
            )
        )
    return int(busy or 0)


def closes_failed(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """What ``spawn`` logged of the closes of work cut off that failed."""
    said = f"Background task {uow_module.CLOSE_TASK!r} failed"
    logged = [r for r in caplog.records if r.name == "domainkit.tasks"]
    return [r for r in logged if r.getMessage() == said]


def close_task() -> "asyncio.Task[None]":
    """The task closing the session of work cut off, still running."""
    [closing] = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == uow_module.CLOSE_TASK and not task.done()
    ]
    return closing


@pytest.mark.parametrize(
    ("path", "middleware"),
    [
        ("/slow-pay", ("domainkit_tests.deadline_routes.InATask",)),
        ("/slow-pay-in-a-group?group=anyio", ()),
        ("/slow-pay-in-a-group?group=asyncio", ()),
    ],
    ids=[
        "under-base-http-middleware",
        "in-an-anyio-task-group",
        "in-an-asyncio-task-group",
    ],
)
@BOUNDED
async def test_work_cut_off_gives_its_connection_back_under_any_cancellation(
    database_url: str, path: str, middleware: tuple[str, ...]
) -> None:
    """The pool has its connection back when the 503 is sent, on time.

    So it is under an anyio cancel scope, which cancels every wait until it
    exits (the task group a ``BaseHTTPMiddleware`` serves the route in, or
    one the route opens), and in an asyncio task group, which cancels once.
    """
    async with paying(
        database_url, middleware=middleware, REQUEST_TIMEOUT=30.0
    ) as client:
        for _ in range(3):
            answer, took = await cut_off(client, path)
            assert answer.status_code == 503
            assert answer.json()["code"] == "deadline_exceeded"
            assert took < 1.0  # The close waited for is a rollback.
            assert checked_out() == 0
        assert await rows(payments) == []
        if ON_POSTGRES:
            assert await busy_sessions() == 0  # Rolled back, not left open.
        deadline_routes.hang_payments[:] = [False]
        assert (await client.post("/slow-pay", json={})).status_code == 201
        assert checked_out() == 0


@BOUNDED
async def test_work_that_goes_on_after_its_cancellation_still_closes(
    database_url: str,
) -> None:
    """A block that catches an anyio scope's cancellation and ends still closes.

    The scope would cancel the close too: the unit of work sees that a
    cancellation is pending (``Task.cancelling``), not only one it is
    handed.
    """
    async with paying(database_url):
        async with asyncio.timeout(10):
            with anyio.CancelScope() as scope:
                async with PaymentsUnitOfWork() as uow:
                    await uow.session.execute(insert(payments))
                    await uow.session.flush()
                    scope.cancel()
                    with suppress(asyncio.CancelledError):
                        await asyncio.sleep(10)
        assert checked_out() == 0
        assert await rows(payments) == []


@BOUNDED
async def test_a_rollback_slower_than_the_bound_discards_the_connection(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ROLLBACK`` that does not answer within ``SETTLE_TIMEOUT`` is given up on.

    The close hangs on a driver that will not answer (aiosqlite whose
    thread the cut stopped is the real case). At the bound the connection
    is invalidated, which takes it out of the pool at once, and the close
    is cancelled; the cancellation lands in the ``ROLLBACK``. On
    PostgreSQL the cancellation reaches ``_keep_cut_off``, which must not
    settle and keep a connection already invalidated: it is discarded, the
    pool opens a fresh one, and the server is left with no busy session of
    its own.
    """
    monkeypatch.setattr(uow_module, "SETTLE_TIMEOUT", 0.2)
    answering = asyncio.Event()  # Never set: the ROLLBACK does not answer.
    rolling: list[asyncio.Task[Any] | None] = []
    async with paying(database_url, REQUEST_TIMEOUT=30.0) as client:
        dialect = connections.engine().sync_engine.dialect

        def hung(dbapi_connection: Any) -> None:  # noqa: ANN401 - the driver's.
            rolling.append(asyncio.current_task())  # The close's.
            if ON_POSTGRES:
                # A real statement in flight, so the bound's cancellation
                # lands in asyncpg and reaches _keep_cut_off.
                driver = dbapi_connection.driver_connection
                await_only(driver.execute("SELECT pg_sleep(1)"))
            else:
                await_only(answering.wait())  # pragma: no cover - never answers.

        with monkeypatch.context() as patched:
            patched.setattr(dialect, "do_rollback", hung)
            answer, took = await cut_off(client, "/slow-pay")
            assert answer.status_code == 503
            assert 0.2 <= took < 2.0  # Answered at the bound.
            assert checked_out() == 0  # Invalidated, out of the pool.
            [task] = rolling
            assert task is not None
            assert task.get_name() == uow_module.CLOSE_TASK
            async with asyncio.timeout(5):
                await asyncio.wait([task])
            assert task.cancelled()
        assert checked_out() == 0
        if ON_POSTGRES:
            for _ in range(100):  # The orphaned backend ends; none is kept.
                if await busy_sessions() == 0:
                    break
                await asyncio.sleep(0.1)
            assert await busy_sessions() == 0
        deadline_routes.hang_payments[:] = [False]
        assert (await client.post("/slow-pay", json={})).status_code == 201
        assert checked_out() == 0


@BOUNDED
async def test_a_close_that_fails_is_logged_and_the_cut_still_answered(
    database_url: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancellation goes on: 503, not the close's error; that is logged."""
    real_close = AsyncSession.close

    async def failing_close(session: AsyncSession) -> None:
        await real_close(session)
        raise RuntimeError("The close failed.")

    caplog.set_level(logging.ERROR, "domainkit.tasks")
    async with paying(database_url, REQUEST_TIMEOUT=30.0) as client:
        with monkeypatch.context() as patched:
            patched.setattr(AsyncSession, "close", failing_close)
            answer, _ = await cut_off(client, "/slow-pay")
        assert answer.status_code == 503
        assert answer.json()["code"] == "deadline_exceeded"
        assert checked_out() == 0
    [failed] = closes_failed(caplog)
    assert failed.exc_info is not None
    assert str(failed.exc_info[1]) == "The close failed."


@BOUNDED
async def test_work_cancelled_again_as_it_closes_leaves_at_once(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asyncio cancels once each time: another cancellation ends the wait at once.

    The close is not cut off with it: it ends on its own, and gives the
    connection back.
    """
    closing_started = asyncio.Event()
    answering = asyncio.Event()
    written = asyncio.Event()
    real_close = AsyncSession.close

    async def slow_close(session: AsyncSession) -> None:
        closing_started.set()
        await answering.wait()
        await real_close(session)

    async def pay() -> None:
        async with PaymentsUnitOfWork() as uow:
            await uow.session.execute(insert(payments))
            await uow.session.flush()
            written.set()
            await asyncio.Event().wait()

    async with paying(database_url):
        with monkeypatch.context() as patched:
            patched.setattr(AsyncSession, "close", slow_close)
            async with asyncio.timeout(10):
                work = asyncio.create_task(pay())
                await written.wait()
                work.cancel()  # The cut.
                await closing_started.wait()
                work.cancel()  # Again, while it closes.
                with pytest.raises(asyncio.CancelledError):
                    await work
                assert checked_out() == 1  # Still closing.
                closing = close_task()
                answering.set()
                await closing
        assert checked_out() == 0
        assert await rows(payments) == []


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


@postgres_only
async def test_a_deadline_longer_than_postgresql_takes_asks_for_the_most(
    database_url: str,
) -> None:
    """35 days without STATEMENT_TIMEOUT: 24.8 days, the most it takes, not an error."""
    with freeze_time("2026-01-01"):
        async with paying(
            database_url, {"STATEMENT_TIMEOUT": None}, REQUEST_TIMEOUT=5.0
        ) as client:
            response = await client.get("/month-long-statement-timeout")

    assert response.status_code == 200
    assert _milliseconds(response.json()["statement_timeout"]) == 2_147_483_647


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


@pytest.mark.usefixtures("database_access")
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
