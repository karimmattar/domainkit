"""The pytest plugin that projects enable with ``-p domainkit.testing``."""

import asyncio
import io
import os
import sys
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import anyio
import anyio.abc
import anyio.from_thread
import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import RedirectResponse
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.types import Receive, Scope, Send

# The pytester fixture unloads the modules first imported while it is in use.
# Alembic keeps global state in its context module, and the routes the inner
# tests serve define tables (the shop's, the idempotency keys): load them
# beforehand, or a second run in the same process defines them again.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_auth_routes
import domainkit_tests.sample_routes  # noqa: F401
from domainkit import clock
from domainkit import testing as plugin
from domainkit.conf import override_settings, settings
from domainkit.conf.env import project_prefix
from domainkit.conf.global_settings import DatabaseConfig
from domainkit.testing import (
    IN_MEMORY,
    AsyncTestClient,
    TestClient,
    freeze_time,
    migrated_database,
)
from domainkit.testing import databases as databases_module
from domainkit.testing.clients import CarryingPortal
from domainkit.testing.databases import (
    WORKER_VARIABLE,
    name_test_database,
    plan_databases,
    worker_id,
)
from domainkit_tests.conftest import MIDDLEWARE, POSTGRES_VARIABLE, on_server


@pytest.fixture
def prefix(monkeypatch: pytest.MonkeyPatch) -> str:
    """Restore the variables the plugin sets once the test ends.

    The nested session is not a pytest-xdist worker, even when this one is:
    it would name its databases after this worker (``test_x_gw1``). A test
    that wants a worker sets ``PYTEST_XDIST_WORKER`` itself.
    """
    prefix = project_prefix()
    for name in ("SETTINGS_MODULE", "ENV_FILE"):
        monkeypatch.delenv(f"{prefix}{name}", raising=False)
    monkeypatch.delenv(WORKER_VARIABLE, raising=False)
    return prefix


def test_projects_get_a_database_and_a_client(
    pytester: pytest.Pytester, tmp_path: Path, prefix: str
) -> None:
    """The fixtures migrate a scratch database and serve the application."""
    (tmp_path / "migrations").mkdir()
    pytester.makepyfile(
        test_project="""
        def test_client(client, database):
            assert database.endswith("test.sqlite3")
            assert client.get("/ok").status_code == 200
        """
    )
    with override_settings(
        MIGRATIONS_DIR=tmp_path / "migrations",
        DATABASES={"default": {"URL": IN_MEMORY}},
    ):
        result = pytester.runpytest_inprocess(
            "-p",
            "domainkit.testing",
            "-p",
            "no:randomly",
            "-p",
            "no:cov",
            "-o",
            "domainkit_settings=project.settings.test",
        )

    result.assert_outcomes(passed=1)
    assert os.environ[f"{prefix}SETTINGS_MODULE"] == "project.settings.test"
    assert os.environ[f"{prefix}ENV_FILE"] == ""


def test_the_ini_option_is_described(pytester: pytest.Pytester) -> None:
    """``pytest --help`` explains ``domainkit_settings``."""
    result = pytester.runpytest_inprocess("-p", "domainkit.testing", "--help")

    result.stdout.fnmatch_lines(
        [
            "  domainkit_settings (string):",
            "* Settings module for the tests (config.settings.test)",
        ]
    )


def test_without_the_ini_option_the_settings_are_left_alone(
    pytester: pytest.Pytester, prefix: str
) -> None:
    """Projects may select their settings some other way."""
    pytester.makepyfile(test_nothing="def test_nothing():\n    pass\n")

    result = pytester.runpytest_inprocess(
        "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
    )

    result.assert_outcomes(passed=1)
    assert f"{prefix}SETTINGS_MODULE" not in os.environ


@pytest.mark.usefixtures("database_access")
def test_a_named_database_keeps_its_url_and_options(tmp_path: Path) -> None:
    """Only in-memory SQLite is replaced; other options survive."""
    (tmp_path / "migrations").mkdir()
    url = f"sqlite+aiosqlite:///{tmp_path / 'named.sqlite3'}"
    with (
        override_settings(
            MIGRATIONS_DIR=tmp_path / "migrations",
            DATABASES={"default": {"URL": url, "STATEMENT_TIMEOUT": 5}},
        ),
        migrated_database(tmp_path) as used,
    ):
        assert used == url
        assert settings.DATABASES["default"].STATEMENT_TIMEOUT == 5


def test_memory_caches_start_empty_in_every_test(
    pytester: pytest.Pytester, prefix: str
) -> None:
    """The plugin empties memory caches before each test, whatever ran before."""
    pytester.makepyfile(
        test_caches="""
        import asyncio
        from domainkit.cache import caches

        def test_writes():
            async def write():
                await caches["default"].set("k", 1)
            asyncio.run(write())

        def test_reads():
            async def read():
                return await caches["default"].get("k")
            assert asyncio.run(read()) is None
        """
    )

    result = pytester.runpytest_inprocess(
        "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
    )

    result.assert_outcomes(passed=2)


def test_outboxes_start_empty_in_every_test(
    pytester: pytest.Pytester, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mail, SMS, and push sent in one test are gone in the next, as in Django."""
    # A backend module nothing imported is passed over, not imported.
    monkeypatch.setattr(
        plugin,
        "OUTBOXES",
        (("domainkit_tests.never_imported", "outbox"), *plugin.OUTBOXES),
    )
    pytester.makepyfile(
        test_outboxes="""
        from domainkit.contrib import mail
        from domainkit.contrib.notifications import backends

        OUTBOXES = (mail.outbox, backends.sms_outbox, backends.push_outbox)

        def test_sends():
            for outbox in OUTBOXES:
                outbox.append("sent")

        def test_sees_none():
            assert OUTBOXES == ([], [], [])
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)
    assert "domainkit_tests.never_imported" not in sys.modules


def test_factories_number_from_zero_in_every_test(
    pytester: pytest.Pytester, prefix: str
) -> None:
    """Every factory, subclasses of factories too, starts again at 0."""
    pytester.makepyfile(
        test_sequences="""
        from dataclasses import dataclass

        from domainkit.testing import Factory, Sequence

        @dataclass
        class Thing:
            name: str

        class ThingFactory(Factory[Thing]):
            class Meta:
                model = Thing

            name = Sequence(lambda n: f"thing-{n}")

        class OtherThingFactory(ThingFactory):
            pass

        def test_first():
            assert [t.name for t in ThingFactory.build_batch(2)] == [
                "thing-0",
                "thing-1",
            ]
            assert OtherThingFactory.build_batch(2)[1].name == "thing-1"

        def test_second():
            assert ThingFactory.build().name == "thing-0"
            assert OtherThingFactory.build().name == "thing-0"
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)


def test_reports_and_logs_are_written_after_every_test(
    pytester: pytest.Pytester, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each test's error reports and logs are written before the next one runs."""
    flushed: list[str] = []
    monkeypatch.setattr(
        "domainkit.testing.flush_reports", lambda: flushed.append("reports")
    )
    monkeypatch.setattr("domainkit.testing.flush_logs", lambda: flushed.append("logs"))
    pytester.makepyfile(
        test_two="""
        def test_one():
            pass

        def test_two():
            pass
        """
    )

    result = pytester.runpytest_inprocess(
        "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
    )

    result.assert_outcomes(passed=2)
    assert flushed == ["reports", "logs", "reports", "logs"]


def test_clients_for_users_and_superusers(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """``client_for`` and ``admin_client`` authenticate as new users.

    ``auth_project`` comes first: it loads the database driver and writes the
    migrations before pytester takes its snapshot of the loaded modules.
    """
    pytester.makepyfile(
        test_users="""
        def test_clients(client, client_for, admin_client):
            assert client.get("/private").status_code == 401
            viewer = client_for("shop.view_product")
            assert viewer.get("/products").json()["user"].startswith("user-")
            assert viewer.post("/products").status_code == 403
            other = client_for("shop.view_product")
            assert other.get("/private").json() != viewer.get("/private").json()
            staff = client_for(username="carol", is_staff=True)
            assert staff.get("/staff").status_code == 200
            assert staff.get("/products").status_code == 403
            me = admin_client.get("/auth/me").json()
            assert (me["is_superuser"], me["is_staff"]) == (True, False)
            assert admin_client.get("/admin/auth/users").status_code == 200
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = pytester.runpytest_inprocess(
            "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
        )

    result.assert_outcomes(passed=1)


def test_every_test_starts_from_the_migrated_database(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """What a test commits is gone in the next; the rows ``migrate`` wrote stay.

    Without the reset, the second ``carol`` would be a taken username.
    """
    pytester.makepyfile(
        test_isolation="""
        from sqlalchemy import func, select, table

        from domainkit.db import connections
        from domainkit.management.base import run_async

        def count(name):
            async def read():
                query = select(func.count()).select_from(table(name))
                async with connections.engine().connect() as connection:
                    return await connection.scalar(query)
            return run_async(read())

        def test_carol(client_for):
            assert client_for("shop.view_product", username="carol")
            assert count("domainkit_users") == 1

        def test_carol_again(client_for):
            assert client_for(username="carol")

        def test_without_a_client(db):
            assert count("domainkit_users") == 0
            assert count("domainkit_tokens") == 0
            assert count("domainkit_permissions") > 0
            assert count("domainkit_content_types") > 0
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = pytester.runpytest_inprocess(
            "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
        )

    result.assert_outcomes(passed=3)


def run_plugin(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    """Run the inner session with the plugin, in file order, without coverage."""
    return pytester.runpytest_inprocess(
        "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov", *args
    )


def test_async_tests_use_the_sync_and_async_clients(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """Every client fixture works in tests AnyIO runs (the sync ones too)."""
    pytester.makepyfile(
        test_async="""
        import pytest

        pytestmark = pytest.mark.anyio

        async def test_sync_clients(client, client_for, admin_client):
            assert client.get("/private").status_code == 401
            assert client_for("shop.view_product").get("/products").status_code == 200
            assert admin_client.get("/admin/auth/users").status_code == 200

        async def test_async_clients(
            async_client, async_client_for, async_admin_client
        ):
            assert (await async_client.get("/private")).status_code == 401
            viewer = await async_client_for("shop.view_product")
            assert (await viewer.get("/products")).json()["user"].startswith("user-")
            assert (await viewer.post("/products")).status_code == 403
            staff = await async_client_for(username="carol", is_staff=True)
            assert (await staff.get("/staff")).status_code == 200
            me = (await async_admin_client.get("/auth/me")).json()
            assert (me["is_superuser"], me["is_staff"]) == (True, False)
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = run_plugin(pytester)

    result.assert_outcomes(passed=2)


def test_clients_for_users_share_the_clients_event_loop(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """Their requests run on the ``client`` fixture's loop: one set of engines."""
    pytester.makepyfile(
        test_loops="""
        import asyncio

        from domainkit.db import connections

        def test_one_loop(client, client_for, admin_client):
            viewer = client_for("shop.view_product")
            assert viewer.portal is admin_client.portal is client.portal
            loops = set()
            pool = connections._pool

            def spy(alias):
                loops.add(asyncio.get_running_loop())
                return pool(alias)

            connections._pool = spy
            try:
                for _ in range(3):
                    assert viewer.get("/products").status_code == 200
                    assert admin_client.get("/admin/auth/users").status_code == 200
            finally:
                del connections._pool
            assert len(loops) == 1
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = run_plugin(pytester)

    result.assert_outcomes(passed=1)


def test_the_clock_freezes_until_the_test_ends(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """``freeze_time`` reaches the application, then lets go after the test.

    So do the changes of the ``settings`` fixture.
    """
    pytester.makepyfile(
        test_frozen="""
        from datetime import UTC, datetime, timedelta

        import pytest

        from domainkit import clock
        from domainkit.conf import settings as current

        NEW_YEAR = datetime(2030, 1, 1, tzinfo=UTC)

        def test_sync(freeze_time, client_for, settings):
            settings.SHUTDOWN_TIMEOUT = 3
            assert current.SHUTDOWN_TIMEOUT == 3
            frozen = freeze_time("2030-01-01")
            viewer = client_for("shop.view_product")
            assert clock.now() == NEW_YEAR
            frozen.tick(timedelta(days=14))
            assert viewer.get("/products").json()["detail"] == "Token expired."

        @pytest.mark.anyio
        async def test_async(freeze_time, async_client_for):
            frozen = freeze_time(NEW_YEAR, tick=True)
            viewer = await async_client_for("shop.view_product")
            assert (await viewer.get("/products")).status_code == 200
            with freeze_time(NEW_YEAR + timedelta(days=15)):
                expired = await viewer.get("/products")
            assert expired.json()["detail"] == "Token expired."
            assert frozen.now() >= NEW_YEAR
            assert (await viewer.get("/products")).status_code == 200

        def test_after():
            assert clock.frozen() is None
            assert current.SHUTDOWN_TIMEOUT == 15

        @pytest.mark.anyio
        async def test_after_in_a_task():
            assert clock.frozen() is None
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = run_plugin(pytester)

    result.assert_outcomes(passed=4)


def test_query_counts_are_checked(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """The query fixtures count the statements a request runs, and list them."""
    pytester.makepyfile(
        test_queries="""
        import pytest

        from domainkit.contrib.auth.principals import invalidate_all

        def test_counts(client_for, assert_num_queries, assert_max_queries):
            viewer = client_for("shop.view_product")
            viewer.get("/products")  # A token's first use is recorded, once.
            with assert_num_queries(0):
                viewer.get("/products")  # Its principal is kept: no query.
            invalidate_all()  # Read it again: a request with queries to count.
            with assert_max_queries(20) as queries:
                assert viewer.get("/products").status_code == 200
            invalidate_all()
            with assert_num_queries(len(queries)):
                viewer.get("/products")
            invalidate_all()
            with pytest.raises(AssertionError) as failure:
                with assert_num_queries(0):
                    viewer.get("/products")
            message = str(failure.value)
            assert message.startswith(f"Expected 0 queries, {len(queries)} ran:")
            assert "\\n1. SELECT " in message
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = run_plugin(pytester)

    result.assert_outcomes(passed=1)


def test_keepdb_is_described(pytester: pytest.Pytester) -> None:
    """``pytest --help`` explains ``--keepdb``."""
    result = pytester.runpytest_inprocess("-p", "domainkit.testing", "--help")

    result.stdout.fnmatch_lines(
        [
            "domainkit:",
            "  --keepdb * Keep the PostgreSQL test databases for the next run, and",
            "* only migrate them up.",
        ]
    )


NEW_YEAR = datetime(2030, 1, 1, tzinfo=UTC)


def clock_app(events: list[str]) -> FastAPI:
    """An application that tells the time, with a lifespan that keeps state."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, str]]:
        events.append("startup")
        yield {"greeting": "hi"}
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/now")
    async def now(request: Request) -> dict[str, str]:
        return {
            "now": clock.now().isoformat(),
            "greeting": getattr(request.state, "greeting", ""),
            "header": request.headers.get("x-test", ""),
            "agent": request.headers["user-agent"],
            "host": request.headers["host"],
            "client": f"{request.client.host}:{request.client.port}"
            if request.client
            else "",
        }

    @app.get("/then")
    async def then() -> RedirectResponse:
        return RedirectResponse("/now")

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, str]:
        return {"body": (await request.body()).decode()}

    @app.websocket("/now")
    async def now_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(clock.now().isoformat())
        await websocket.close()

    return app


def test_the_sync_client_carries_the_callers_clock() -> None:
    """Requests and WebSocket sessions read the caller's frozen clock."""
    events: list[str] = []
    with TestClient(clock_app(events)) as client, freeze_time(NEW_YEAR) as frozen:
        assert client.get("/now").json()["now"] == NEW_YEAR.isoformat()
        frozen.tick(60)
        with client.websocket_connect("/now") as socket:
            assert socket.receive_text() == "2030-01-01T00:01:00+00:00"
        other = client.another({"x-test": "1"})
        assert other.get("/now").json() == {
            "now": "2030-01-01T00:01:00+00:00",
            "greeting": "hi",
            "header": "1",
            "agent": "testclient",
            "host": "testserver",
            "client": "testclient:50000",
        }
        assert other.portal is client.portal
    assert events == ["startup", "shutdown"]
    assert client.get("/now").json()["now"] != NEW_YEAR.isoformat()


def test_clients_share_only_an_entered_clients_loop() -> None:
    """Without a running loop to share, ``another`` and ``authenticated`` refuse."""
    client = TestClient(clock_app([]))
    with freeze_time(NEW_YEAR):  # A client used without entering still carries it.
        assert client.get("/now").json()["now"] == NEW_YEAR.isoformat()
    refusal = r"^Enter the client first: with TestClient\(app\) as \.\.\.$"
    with pytest.raises(RuntimeError, match=refusal):
        client.another({})
    with pytest.raises(RuntimeError, match=refusal):
        client.authenticated("shop.view_product")


@pytest.mark.anyio
async def test_the_async_client_runs_the_lifespan_in_this_loop() -> None:
    """Startup before the requests, shutdown after; requests see the state."""
    events: list[str] = []
    async with AsyncTestClient(clock_app(events)) as client:
        assert events == ["startup"]
        with freeze_time(NEW_YEAR):
            body = (await client.get("/now")).json()
        assert body == {
            "now": NEW_YEAR.isoformat(),
            "greeting": "hi",
            "header": "",
            "agent": "testclient",
            "host": "testserver",
            "client": "testclient:50000",
        }
        assert (await client.get("/then")).json()["greeting"] == "hi"
        assert (await client.post("/echo", content=b"hello")).json() == {
            "body": "hello"
        }
        async with client.another({"x-test": "2"}) as other:  # No second lifespan.
            assert (await other.get("/now")).json()["header"] == "2"
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]

    async with AsyncTestClient(clock_app(events), lifespan=False):
        pass
    assert events == ["startup", "shutdown"]


class BrokenLifespan:
    """An ASGI application whose lifespan goes wrong in one ``phase``."""

    def __init__(self, phase: str, how: str) -> None:
        """Fail ``phase`` by raising, answering ``failed``, or returning."""
        self.phase = phase
        self.how = how

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer each phase until the broken one."""
        while True:
            message = await receive()
            phase = message["type"].removeprefix("lifespan.")
            if phase != self.phase:
                await send({"type": f"lifespan.{phase}.complete"})
                continue
            if self.how == "raises":
                raise LookupError(f"no {phase}")
            if self.how == "fails":
                await send({"type": f"lifespan.{phase}.failed", "message": "nope"})
            if self.how == "fails quietly":
                await send({"type": f"lifespan.{phase}.failed"})
            return


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["startup", "shutdown"])
@pytest.mark.parametrize(
    ("how", "error", "match"),
    [
        ("raises", LookupError, "no "),
        ("fails", RuntimeError, "lifespan .* failed: nope$"),
        ("fails quietly", RuntimeError, "lifespan .* failed: $"),
        ("returns", RuntimeError, "lifespan .* failed: it stopped"),
    ],
)
async def test_lifespan_failures_are_raised(
    phase: str, how: str, error: type[Exception], match: str
) -> None:
    """What stops the application from starting or stopping reaches the test."""
    client = AsyncTestClient(BrokenLifespan(phase, how))
    with pytest.raises(error, match=match):
        async with client:
            pass


def test_worker_ids_come_from_pytest_xdist(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PYTEST_XDIST_WORKER`` names the worker; xdist itself is not needed."""
    monkeypatch.delenv(WORKER_VARIABLE, raising=False)
    assert worker_id() is None
    monkeypatch.setenv(WORKER_VARIABLE, "")
    assert worker_id() is None
    monkeypatch.setenv(WORKER_VARIABLE, "gw12")
    assert worker_id() == "gw12"
    for bad in ('gw"; DROP', "w" * 33):
        monkeypatch.setenv(WORKER_VARIABLE, bad)
        with pytest.raises(pytest.UsageError, match=WORKER_VARIABLE):
            worker_id()


SERVER = "postgresql+asyncpg://owner:s3cret@db.internal:5432"


@pytest.mark.parametrize(
    ("entry", "worker", "expected"),
    [
        ({"URL": f"{SERVER}/shop"}, None, "test_shop"),
        ({"URL": f"{SERVER}/shop"}, "gw1", "test_shop_gw1"),
        ({"URL": f"{SERVER}/shop", "TEST_NAME": "test_ci"}, None, "test_ci"),
        ({"URL": f"{SERVER}/shop", "TEST_NAME": "test_ci"}, "gw0", "test_ci_gw0"),
        ({"URL": SERVER, "TEST_NAME": "test_ci"}, None, "test_ci"),
        ({"URL": f"{SERVER}/{'s' * 58}"}, None, f"test_{'s' * 58}"),
    ],
)
def test_test_databases_are_named_for_the_configured_one(
    entry: dict[str, str], worker: str | None, expected: str
) -> None:
    """``test_<name>`` (or ``TEST_NAME``), and the worker's id under xdist."""
    config = DatabaseConfig.model_validate(entry)

    assert name_test_database("default", config, worker) == expected


@pytest.mark.parametrize(
    ("entry", "worker", "match"),
    [
        ({"URL": f"{SERVER}/shop", "TEST_NAME": "shop"}, None, "refusing .*'shop'"),
        ({"URL": f"{SERVER}/shop", "TEST_NAME": "shop_test"}, "gw0", "refusing"),
        ({"URL": SERVER}, None, "names no database"),
        ({"URL": f"{SERVER}/{'s' * 59}"}, None, "longer than PostgreSQL's 63"),
        ({"URL": f"{SERVER}/{'s' * 55}"}, "gw10", "set a shorter TEST_NAME"),
    ],
)
def test_names_that_are_not_test_names_are_refused(
    entry: dict[str, str], worker: str | None, match: str
) -> None:
    """Only ``test_*`` databases are ever created or dropped, whole names only."""
    config = DatabaseConfig.model_validate(entry)

    with pytest.raises(pytest.UsageError, match=match) as refusal:
        name_test_database("default", config, worker)
    assert "s3cret" not in str(refusal.value)


def test_each_entry_points_at_its_test_database(tmp_path: Path) -> None:
    """PostgreSQL URLs are renamed, memory SQLite becomes files, others stay."""
    databases = {
        "default": DatabaseConfig.model_validate(
            {"URL": f"{SERVER}/shop", "MIGRATION_URL": f"{SERVER}/shop", "ECHO": True}
        ),
        "replica": DatabaseConfig.model_validate(
            {"URL": "postgresql+asyncpg://reader@db.internal:5432/shop"}
        ),
        "elsewhere": DatabaseConfig.model_validate(
            {"URL": "postgresql+asyncpg://owner@db2.internal:5432/shop"}
        ),
        "reports": DatabaseConfig.model_validate(
            {"URL": f"{SERVER}/reports", "TEST_NAME": "test_reports"}
        ),
        "cache": DatabaseConfig.model_validate(
            {"URL": IN_MEMORY, "MIGRATION_URL": IN_MEMORY}
        ),
        "files": DatabaseConfig.model_validate(
            {"URL": f"sqlite+aiosqlite:///{tmp_path / 'kept.sqlite3'}"}
        ),
    }

    replaced, created = plan_databases(databases, tmp_path, "gw2")

    urls = {
        alias: (
            config.URL.get_secret_value(),
            config.MIGRATION_URL and config.MIGRATION_URL.get_secret_value(),
        )
        for alias, config in replaced.items()
    }
    assert urls == {
        "default": (f"{SERVER}/test_shop_gw2", f"{SERVER}/test_shop_gw2"),
        "replica": ("postgresql+asyncpg://reader@db.internal:5432/test_shop_gw2", None),
        "elsewhere": (
            "postgresql+asyncpg://owner@db2.internal:5432/test_shop_gw2",
            None,
        ),
        "reports": (f"{SERVER}/test_reports_gw2", None),
        "cache": (f"sqlite+aiosqlite:///{tmp_path / 'test_cache.sqlite3'}",) * 2,
        "files": (f"sqlite+aiosqlite:///{tmp_path / 'kept.sqlite3'}", None),
    }
    assert replaced["default"].ECHO is True
    assert [(database.name, database.config) for database in created] == [
        ("test_shop_gw2", replaced["default"]),
        ("test_shop_gw2", replaced["elsewhere"]),
        ("test_reports_gw2", replaced["reports"]),
    ]


postgres_only = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE),
    reason=f"set {POSTGRES_VARIABLE} to a PostgreSQL server",
)


def databases_named(name: str) -> list[str]:
    """The server's databases whose names contain ``name``."""

    async def read() -> list[str]:
        engine = create_async_engine(os.environ[POSTGRES_VARIABLE])
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE :name"),
                    {"name": f"%{name}%"},
                )
                return sorted(row[0] for row in rows)
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, read()).result()


def on_database(name: str, statement: str) -> None:
    """Run ``statement`` in the server's database ``name``."""
    server = make_url(os.environ[POSTGRES_VARIABLE])
    on_server(
        server.set(database=name).render_as_string(hide_password=False), statement
    )


@postgres_only
def test_postgresql_tests_get_a_database_of_their_own(
    auth_project: str,
    prefix: str,
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``test_<name>_<worker>`` replaces what an earlier run left, then goes.

    The configured database does not even exist: it is never touched.
    """
    name = f"dk_{uuid4().hex[:12]}"
    server = make_url(os.environ[POSTGRES_VARIABLE])
    on_server(os.environ[POSTGRES_VARIABLE], f'CREATE DATABASE "test_{name}_gw1"')
    on_database(f"test_{name}_gw1", "CREATE TABLE leftover (id integer)")
    monkeypatch.setenv(WORKER_VARIABLE, "gw1")
    pytester.makepyfile(
        test_lifecycle=f"""
        from sqlalchemy import inspect, text

        from domainkit.db import connections
        from domainkit.management.base import run_async

        def test_database(client_for):
            async def read():
                async with connections.engine().connect() as connection:
                    current = await connection.scalar(text("SELECT current_database()"))
                    tables = await connection.run_sync(
                        lambda sync: inspect(sync).get_table_names()
                    )
                    return current, tables
            current, tables = run_async(read())
            assert current == "test_{name}_gw1"
            assert "leftover" not in tables and "domainkit_users" in tables
            assert client_for("shop.view_product").get("/products").status_code == 200
        """
    )
    try:
        with override_settings(
            DATABASES={
                "default": {
                    "URL": server.set(database=name).render_as_string(
                        hide_password=False
                    )
                }
            },
            ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
            MIDDLEWARE=MIDDLEWARE[:3],
        ):
            result = run_plugin(pytester)

        result.assert_outcomes(passed=1)
        assert databases_named(name) == []
    finally:
        on_server(
            os.environ[POSTGRES_VARIABLE],
            f'DROP DATABASE IF EXISTS "test_{name}_gw1" WITH (FORCE)',
        )


@postgres_only
def test_keepdb_keeps_the_database_for_the_next_run(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """The next run reuses it, from the rows ``migrate`` left: carol is gone."""
    name = f"dk_{uuid4().hex[:12]}"
    server = make_url(os.environ[POSTGRES_VARIABLE])
    pytester.makepyfile(
        test_kept="""
        def test_carol(client_for):
            assert client_for(username="carol").get("/private").status_code == 200
        """
    )
    try:
        with override_settings(
            DATABASES={
                "default": {
                    "URL": server.set(database=name).render_as_string(
                        hide_password=False
                    ),
                    "TEST_NAME": f"test_{name}_kept",
                }
            },
            ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
            MIDDLEWARE=MIDDLEWARE[:3],
        ):
            for _ in range(2):
                run_plugin(pytester, "--keepdb").assert_outcomes(passed=1)
                assert databases_named(name) == [f"test_{name}_kept"]
    finally:
        on_server(
            os.environ[POSTGRES_VARIABLE],
            f'DROP DATABASE IF EXISTS "test_{name}_kept" WITH (FORCE)',
        )


def test_portal_tasks_start_with_the_callers_context() -> None:
    """Tasks the portal starts get the arguments, the name, and the context."""
    seen: list[object] = []

    async def task(
        first: int, second: int, *, task_status: anyio.abc.TaskStatus[str]
    ) -> None:
        running = asyncio.current_task()
        assert running is not None
        seen.extend([first, second, clock.now(), running.get_name()])
        task_status.started("ready")

    with (
        anyio.from_thread.start_blocking_portal() as portal,
        freeze_time(NEW_YEAR),
    ):
        future, started = CarryingPortal(portal).start_task(task, 1, 2, name="probe")
        future.result()

    assert started == "ready"
    assert seen == [1, 2, NEW_YEAR, "probe"]


def test_portal_tasks_started_soon_carry_the_callers_context() -> None:
    """Starlette 1.9 runs each request with ``start_task_soon``: it carries too."""

    async def task(first: int) -> tuple[int, datetime, str]:
        running = asyncio.current_task()
        assert running is not None
        return first, clock.now(), running.get_name()

    def stamp() -> datetime:
        return clock.now()

    with (
        anyio.from_thread.start_blocking_portal() as portal,
        freeze_time(NEW_YEAR),
    ):
        carrying = CarryingPortal(portal)
        assert carrying.start_task_soon(task, 1, name="probe").result() == (
            1,
            NEW_YEAR,
            "probe",
        )
        assert carrying.start_task_soon(stamp).result() == NEW_YEAR


def test_cancelling_a_task_started_soon_cancels_it() -> None:
    """The future cancels the task it stands for, as the portal's own does."""
    with anyio.from_thread.start_blocking_portal() as portal:
        carrying = CarryingPortal(portal)
        started = carrying.call(anyio.Event)
        stopped = carrying.call(anyio.Event)

        async def forever() -> None:
            started.set()
            try:
                await anyio.sleep_forever()
            finally:
                stopped.set()

        future = carrying.start_task_soon(forever)
        carrying.call(started.wait)
        future.cancel()
        carrying.call(stopped.wait)

    assert future.cancelled()


def test_portal_context_managers_carry_the_callers_context() -> None:
    """``wrap_async_context_manager`` enters and exits in the caller's context."""
    seen: list[object] = []

    @asynccontextmanager
    async def manager() -> AsyncIterator[str]:
        seen.append(clock.now())
        try:
            yield "entered"
        except KeyError as error:
            seen.append(error.args)
        finally:
            seen.append(clock.now())

    with (
        anyio.from_thread.start_blocking_portal() as portal,
        freeze_time(NEW_YEAR),
    ):
        carrying = CarryingPortal(portal)
        with carrying.wrap_async_context_manager(manager()) as value:
            assert value == "entered"
        with carrying.wrap_async_context_manager(manager()):
            raise KeyError("suppressed")  # The manager swallows it; so does the wrap.

    assert seen == [NEW_YEAR, NEW_YEAR, NEW_YEAR, ("suppressed",), NEW_YEAR]


def test_the_portals_other_attributes_are_the_wrapped_portals() -> None:
    """Whatever the portal offers beyond starting work reads through."""
    with anyio.from_thread.start_blocking_portal() as portal:
        carrying = CarryingPortal(portal)
        assert carrying.stop == portal.stop
        assert carrying.sleep_until_stopped == portal.sleep_until_stopped
        with pytest.raises(AttributeError, match="no_such_method"):
            carrying.no_such_method  # noqa: B018 - reading it is the test.


def test_errors_name_the_entry(tmp_path: Path) -> None:
    """A refused test database names the ``DATABASES`` entry it comes from."""
    reports = DatabaseConfig.model_validate(
        {"URL": f"{SERVER}/reports", "TEST_NAME": "reports"}
    )

    with pytest.raises(pytest.UsageError, match=r"^DATABASES\['reports'\]: refusing"):
        plan_databases({"reports": reports}, tmp_path, None)


@pytest.mark.parametrize(
    ("keepdb", "calls"),
    [
        (False, [("migrate",), ("migrate", "base", True), ("migrate",)]),
        (True, [("migrate",)]),
    ],
)
@pytest.mark.usefixtures("database_access")
def test_migrations_go_down_and_up_unless_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keepdb: bool,
    calls: list[tuple[object, ...]],
) -> None:
    """Up, down to base, and up again; with ``keepdb``, up only. Quietly."""
    made: list[tuple[object, ...]] = []

    def call_command(*args: str, stdout: io.StringIO, downgrade: bool = False) -> None:
        assert isinstance(stdout, io.StringIO)
        made.append((*args, True) if downgrade else args)

    monkeypatch.setattr(databases_module, "call_command", call_command)
    with (
        override_settings(DATABASES={"default": {"URL": IN_MEMORY}}),
        migrated_database(tmp_path, keepdb=keepdb),
    ):
        pass

    assert made == calls
