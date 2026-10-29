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
    tests that reach the database without a client: a test that did not
    request it is refused the databases (``DatabaseAccessRefused``).
``database_blocker``
    Session-wide: ``with database_blocker.unblock():`` lets a fixture wider
    than one test reach the databases, as pytest-django's
    ``django_db_blocker`` does.
``client``, ``async_client``
    A ``TestClient`` (an ``AsyncTestClient``) for the project's application,
    lifespan included, on the ``db`` databases. Requests send no credentials.
``live_server``
    ``live_server.url``: the application served by uvicorn on a port of the
    test process, for browsers and other processes, on the ``db``
    databases, with ``127.0.0.1`` added to ``ALLOWED_HOSTS``. Started for
    the test, lifespan included, and stopped after it.
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
``settings``
    ``settings.DEBUG = True``: the setting is changed, and validated, until
    the test ends, as ``override_settings`` changes it for a block;
    ``del settings.CACHES`` restores a default.

``@pytest.mark.allow_database`` lets a test reach the databases without
``db``, as every test could until 0.13: they are not put back first. The
ini option ``domainkit_allow_database = true`` does it for every test.

``pytest --detect-blocking`` fails a test that holds its event loop for
``--blocking-seconds`` (0.1) or more in one step (``domainkit.testing.blocking``).

The async fixtures need async tests run by AnyIO (``pytest.mark.anyio``).
Memory caches (``memory://``) are emptied before every test, so no test
sees what another one cached. Redis caches are left alone. The memory
backends' outboxes (``mail.outbox``, ``sms_outbox``, ``push_outbox``) are
emptied too, and every ``Factory`` numbers its objects from 0 again. Error
reports and queued logs are sent before the next test starts; call
``domainkit.reporting.flush_reports()`` before checking what a test reported.
"""

import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterator
from contextlib import ExitStack
from typing import Any, Final

import pytest

from domainkit.cache import caches
from domainkit.clock import FrozenClock, Moment, freeze_time
from domainkit.conf import UNSET, override_settings
from domainkit.conf import settings as current_settings
from domainkit.conf.env import project_prefix
from domainkit.db import connections
from domainkit.db.snapshots import Snapshot, restore_snapshot, take_snapshot
from domainkit.http import get_asgi_application
from domainkit.log import flush_logs
from domainkit.management.base import run_async
from domainkit.reporting import flush_reports

from . import blocking
from .clients import AsyncTestClient, TestClient, token_for, user_token
from .databases import IN_MEMORY, REFUSAL, DatabaseBlocker, migrated_database, worker_id
from .factories import Factory, LazyAttribute, LazyFunction, Sequence, SubFactory
from .live import LIVE_HOST, LiveServer
from .queries import CapturedQueries, assert_max_queries, assert_num_queries

__all__ = [
    "IN_MEMORY",
    "AsyncClientFor",
    "AsyncTestClient",
    "CapturedQueries",
    "ClientFor",
    "DatabaseBlocker",
    "Factory",
    "FreezeTime",
    "FrozenClock",
    "LazyAttribute",
    "LazyFunction",
    "LiveServer",
    "Sequence",
    "SettingsOverrides",
    "SubFactory",
    "TestClient",
    "assert_max_queries",
    "assert_num_queries",
    "freeze_time",
    "migrated_database",
    "token_for",
    "user_token",
]

SETTINGS_INI: Final = "domainkit_settings"
ALLOW_DATABASE_INI: Final = "domainkit_allow_database"
ALLOW_DATABASE_MARK: Final = "allow_database"
_REFUSED_BEFORE: Final = pytest.StashKey[str | None]()
"""What ``connections`` refused before the plugin did, put back at the end."""
OUTBOXES: Final = (
    ("domainkit.contrib.mail.backends", "outbox"),
    ("domainkit.contrib.notifications.backends", "sms_outbox"),
    ("domainkit.contrib.notifications.backends", "push_outbox"),
)
"""Where the memory backends keep what they sent: (module, list)."""

type ClientFor = Callable[..., TestClient]
type AsyncClientFor = Callable[..., Awaitable[AsyncTestClient]]
type FreezeTime = Callable[..., FrozenClock]


class SettingsOverrides:
    """The ``settings`` fixture: settings changed until the test ends.

    ``settings.DEBUG = True`` overrides one, validated as
    ``override_settings`` validates it; ``del settings.CACHES`` puts its
    default back. Reading an attribute reads the current setting. The
    changes are undone in reverse order when the test ends, as
    pytest-django's ``settings`` fixture undoes them.
    """

    def __init__(self, stack: ExitStack) -> None:
        """Keep the changes on ``stack``, which the fixture closes."""
        object.__setattr__(self, "_stack", stack)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - any setting.
        """The current value of setting ``name``."""
        return getattr(current_settings, name)

    def __setattr__(self, name: str, value: object) -> None:
        """Change setting ``name`` until the test ends.

        Raises:
            ImproperlyConfigured: The settings refuse the value.
        """
        self._stack.enter_context(override_settings(**{name: value}))

    def __delattr__(self, name: str) -> None:
        """Put setting ``name``'s default back until the test ends."""
        self._stack.enter_context(override_settings(**{name: UNSET}))


def pytest_addoption(parser: pytest.Parser) -> None:
    """Declare the ini options, ``--keepdb``, and ``--detect-blocking``."""
    parser.addini(SETTINGS_INI, "Settings module for the tests (config.settings.test)")
    parser.addini(
        ALLOW_DATABASE_INI,
        "Let every test reach the databases without requesting db, as before "
        "0.13 (false)",
        type="bool",
        default=False,
    )
    group = parser.getgroup("domainkit")
    group.addoption(
        "--keepdb",
        action="store_true",
        help="Keep the PostgreSQL test databases for the next run, and only "
        "migrate them up.",
    )
    blocking.add_options(group)


def pytest_configure(config: pytest.Config) -> None:
    """Select the test settings, keep the ``.env`` out, and refuse the databases.

    The databases stay refused for the whole run, outside the moments a
    fixture or a test asked for them (``database_blocker``).
    """
    prefix = project_prefix()
    if module := config.getini(SETTINGS_INI):
        os.environ[f"{prefix}SETTINGS_MODULE"] = str(module)
    os.environ[f"{prefix}ENV_FILE"] = ""
    config.addinivalue_line(
        "markers",
        f"{ALLOW_DATABASE_MARK}: the test may reach the test databases without "
        f"requesting db; they are not put back first",
    )
    if not config.getini(ALLOW_DATABASE_INI):
        config.stash[_REFUSED_BEFORE] = connections.refusal
        connections.refuse(REFUSAL)
    blocking.configure(config)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give a test marked ``allow_database`` the test databases, first of all.

    So what it reaches are the databases ``database`` made, never the ones
    the settings name, whichever test runs first.
    """
    for item in items:
        if item.get_closest_marker(ALLOW_DATABASE_MARK) is None:
            continue
        names = getattr(item, "fixturenames", None)
        if names is not None and "database" not in names:
            names.insert(0, "database")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    """Allow the databases to a test marked ``allow_database``, as 0.12 did.

    From its first fixture to its last teardown; they are not put back first.
    A hook, not an autouse fixture: the other tests pay nothing for it.
    """
    if item.get_closest_marker(ALLOW_DATABASE_MARK) is None:
        return (yield)
    with DatabaseBlocker().unblock():
        return (yield)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Put back what ``connections`` refused before the run."""
    if _REFUSED_BEFORE in config.stash:
        if (previous := config.stash[_REFUSED_BEFORE]) is None:
            connections.allow()
        else:
            connections.refuse(previous)


@pytest.fixture(autouse=True)
def empty_memory_caches() -> None:
    """Start every test with empty memory caches."""
    caches.reset()


@pytest.fixture(autouse=True)
def empty_outboxes() -> None:
    """Start every test with the memory backends' outboxes empty, as Django does.

    Only those of modules already imported: no other one holds anything.
    """
    for module, name in OUTBOXES:
        if (loaded := sys.modules.get(module)) is not None:
            getattr(loaded, name).clear()


@pytest.fixture(autouse=True)
def fresh_sequences() -> None:
    """Number every factory's objects from 0 again, whatever ran before."""
    factories = Factory.__subclasses__()
    while factories:
        factory = factories.pop()
        factory.reset_sequence()
        factories.extend(factory.__subclasses__())


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
def database_blocker() -> DatabaseBlocker:
    """Refuse the test databases, or allow them for a block."""
    return DatabaseBlocker()


@pytest.fixture(scope="session")
def database(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    database_blocker: DatabaseBlocker,
) -> Iterator[str]:
    """The migrated test database's URL.

    The databases are reached while they are made and dropped, not between.
    """
    keepdb = bool(request.config.getoption("keepdb"))
    scratch = tmp_path_factory.mktemp("database")
    made = migrated_database(scratch, keepdb=keepdb, worker=worker_id())
    with database_blocker.unblock():
        url = made.__enter__()
    try:
        yield url
    finally:
        with database_blocker.unblock():
            made.__exit__(None, None, None)


@pytest.fixture(scope="session")
def _migrated_rows(database: str, database_blocker: DatabaseBlocker) -> Snapshot:
    """Every table's rows, as ``migrate`` left them."""
    with database_blocker.unblock():
        return run_async(take_snapshot())


@pytest.fixture
def db(_migrated_rows: Snapshot, database_blocker: DatabaseBlocker) -> Iterator[None]:
    """Put the test databases back as ``migrate`` left them; allow them in the test."""
    with database_blocker.unblock():
        run_async(restore_snapshot(_migrated_rows))
        yield


@pytest.fixture
def client(db: None) -> Iterator[TestClient]:
    """Serve the project's application on the test database."""
    with TestClient(get_asgi_application()) as test_client:
        yield test_client


@pytest.fixture
def live_server(db: None) -> Iterator[LiveServer]:
    """Serve the project's application on a port of this process, for one test."""
    hosts = [*current_settings.ALLOWED_HOSTS, LIVE_HOST]
    with (
        override_settings(ALLOWED_HOSTS=hosts),
        LiveServer(get_asgi_application()) as server,
    ):
        yield server


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


@pytest.fixture(name="settings")
def _settings() -> Iterator[SettingsOverrides]:
    """Change settings until the test ends: ``settings.DEBUG = True``."""
    with ExitStack() as stack:
        yield SettingsOverrides(stack)


@pytest.fixture(name="assert_num_queries")
def _assert_num_queries() -> Callable[..., CapturedQueries]:
    """``with assert_num_queries(n):`` checks the block runs ``n`` statements."""
    return assert_num_queries


@pytest.fixture(name="assert_max_queries")
def _assert_max_queries() -> Callable[..., CapturedQueries]:
    """``with assert_max_queries(n):`` checks the block runs at most ``n``."""
    return assert_max_queries
