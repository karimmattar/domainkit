"""pytest plugin for domainkit projects: test settings, databases, clients.

Enable it, and name the test settings, in the project's ``pyproject.toml``::

    [tool.pytest.ini_options]
    addopts = "-p domainkit.testing"
    domainkit_settings = "config.settings.test"

The ``.env`` file is never read during tests. The database fixtures empty
every database the settings name, so they refuse settings whose
``ENVIRONMENT`` is not ``"test"``. The fixtures:

``database``
    Session-wide: makes the test databases (``test_<name>`` on PostgreSQL,
    dropped after the run unless ``--keepdb``; a temporary file for
    in-memory SQLite), and migrates them up, down to base, and up again,
    which proves every revision reversible. Yields the default's URL.
``db``
    The migrated databases, put back as ``migrate`` left them before the
    test: whatever earlier tests committed is gone, and the rows
    ``migrate`` wrote (content types, permissions) are there. Request it in
    tests that reach the database without a client.
``client``, ``async_client``
    A ``TestClient`` (an ``AsyncTestClient``) for the project's application,
    lifespan included, on the ``db`` databases. Requests send no credentials.
``client_for``, ``async_client_for``
    ``client_for("shop.view_product", ...)``: a client of the same
    application that authenticates as a new user granted those
    permissions. Keyword arguments set the user's fields
    (``is_staff=True``); the username is unique unless given. Needs
    ``domainkit.contrib.auth``. The async one is awaited.
``admin_client``, ``async_admin_client``
    A client that authenticates as a new superuser.
``freeze_time``
    ``clock = freeze_time("2026-01-01")``: the framework's clock stands at
    that moment (or ticks from it, ``tick=True``) until the test ends;
    ``clock.tick()`` and ``clock.move_to()`` move it.
``assert_num_queries``, ``assert_max_queries``
    ``with assert_num_queries(3):``: fail unless the block runs exactly
    three SQL statements (at most three), listing those it ran.

``pytest --detect-blocking`` fails a test that holds its event loop for
``--blocking-seconds`` (0.1) or more in one step (``domainkit.testing.blocking``).

The async fixtures need async tests run by AnyIO (``pytest.mark.anyio``).
Memory caches (``memory://``) are emptied before every test, so no test
sees what another one cached. Redis caches are left alone. Error reports
and queued logs are sent before the next test starts; call
``domainkit.reporting.flush_reports()`` before checking what a test reported.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, Final

import pytest

from domainkit.cache import caches
from domainkit.clock import FrozenClock, Moment, freeze_time
from domainkit.conf.env import project_prefix
from domainkit.db.snapshots import Snapshot, restore_snapshot, take_snapshot
from domainkit.http import get_asgi_application
from domainkit.log import flush_logs
from domainkit.management.base import run_async
from domainkit.reporting import flush_reports

from . import blocking
from .clients import AsyncTestClient, TestClient, user_token
from .databases import IN_MEMORY, migrated_database, worker_id
from .factories import Factory, LazyAttribute, LazyFunction, Sequence, SubFactory
from .queries import CapturedQueries, assert_max_queries, assert_num_queries

__all__ = [
    "IN_MEMORY",
    "AsyncClientFor",
    "AsyncTestClient",
    "CapturedQueries",
    "ClientFor",
    "Factory",
    "FreezeTime",
    "FrozenClock",
    "LazyAttribute",
    "LazyFunction",
    "Sequence",
    "SubFactory",
    "TestClient",
    "assert_max_queries",
    "assert_num_queries",
    "freeze_time",
    "migrated_database",
    "user_token",
]

SETTINGS_INI: Final = "domainkit_settings"

type ClientFor = Callable[..., TestClient]
type AsyncClientFor = Callable[..., Awaitable[AsyncTestClient]]
type FreezeTime = Callable[..., FrozenClock]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Declare ``domainkit_settings``, ``--keepdb``, and ``--detect-blocking``."""
    parser.addini(SETTINGS_INI, "Settings module for the tests (config.settings.test)")
    group = parser.getgroup("domainkit")
    group.addoption(
        "--keepdb",
        action="store_true",
        help="Keep the PostgreSQL test databases for the next run, and only "
        "migrate them up.",
    )
    blocking.add_options(group)


def pytest_configure(config: pytest.Config) -> None:
    """Select the test settings and keep the developer's ``.env`` out."""
    prefix = project_prefix()
    if module := config.getini(SETTINGS_INI):
        os.environ[f"{prefix}SETTINGS_MODULE"] = str(module)
    os.environ[f"{prefix}ENV_FILE"] = ""
    blocking.configure(config)


@pytest.fixture(autouse=True)
def empty_memory_caches() -> None:
    """Start every test with empty memory caches."""
    caches.reset()


@pytest.fixture(autouse=True)
def reports_and_logs_written() -> Iterator[None]:
    """Send a test's error reports, and write its logs, before the next test.

    Reporters and log handlers run on threads of their own; this keeps what
    one test reported out of the next one's assertions.
    """
    yield
    flush_reports()
    flush_logs()


@pytest.fixture(scope="session")
def database(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """The migrated test database's URL."""
    keepdb = bool(request.config.getoption("keepdb"))
    scratch = tmp_path_factory.mktemp("database")
    with migrated_database(scratch, keepdb=keepdb, worker=worker_id()) as url:
        yield url


@pytest.fixture(scope="session")
def _migrated_rows(database: str) -> Snapshot:
    """Every table's rows, as ``migrate`` left them."""
    return run_async(take_snapshot())


@pytest.fixture
def db(_migrated_rows: Snapshot) -> None:
    """Put the test databases back as ``migrate`` left them."""
    run_async(restore_snapshot(_migrated_rows))


@pytest.fixture
def client(db: None) -> Iterator[TestClient]:
    """Serve the project's application on the test database."""
    with TestClient(get_asgi_application()) as test_client:
        yield test_client


@pytest.fixture
def client_for(client: TestClient) -> ClientFor:
    """Make clients that authenticate as new users with permissions."""
    return client.authenticated


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    """A client that authenticates as a new superuser."""
    return client.authenticated(is_superuser=True)


@pytest.fixture
async def async_client(db: None) -> AsyncIterator[AsyncTestClient]:
    """Serve the project's application in the test's event loop."""
    async with AsyncTestClient(get_asgi_application()) as test_client:
        yield test_client


@pytest.fixture
async def async_client_for(
    async_client: AsyncTestClient,
) -> AsyncIterator[AsyncClientFor]:
    """Make async clients that authenticate as new users with permissions."""
    made: list[AsyncTestClient] = []

    async def make(
        *permissions: str,
        **fields: Any,  # noqa: ANN401 - the user model's fields.
    ) -> AsyncTestClient:
        made.append(await async_client.authenticated(*permissions, **fields))
        return made[-1]

    yield make
    for client in made:
        await client.aclose()


@pytest.fixture
async def async_admin_client(async_client_for: AsyncClientFor) -> AsyncTestClient:
    """An async client that authenticates as a new superuser."""
    return await async_client_for(is_superuser=True)


@pytest.fixture(name="freeze_time")
def _freeze_time() -> Iterator[FreezeTime]:
    """Freeze the framework's clock until the test ends."""
    started: list[FrozenClock] = []

    def freeze(when: Moment | None = None, *, tick: bool = False) -> FrozenClock:
        started.append(freeze_time(when, tick=tick).start())
        return started[-1]

    yield freeze
    for clock in reversed(started):
        clock.stop()


@pytest.fixture(name="assert_num_queries")
def _assert_num_queries() -> Callable[..., CapturedQueries]:
    """``with assert_num_queries(n):`` checks the block runs ``n`` statements."""
    return assert_num_queries


@pytest.fixture(name="assert_max_queries")
def _assert_max_queries() -> Callable[..., CapturedQueries]:
    """``with assert_max_queries(n):`` checks the block runs at most ``n``."""
    return assert_max_queries
