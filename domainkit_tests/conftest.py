"""Fixtures for framework tests: isolated settings, registry, and packages."""

import asyncio
import functools
import importlib
import io
import logging
import os
import shutil
import sys
import textwrap
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from hypothesis import settings as hypothesis_settings
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import (
    on_check_application,
    on_deployment_warnings,
    on_startup,
)
from domainkit.cache import caches
from domainkit.conf import override_settings, use_settings
from domainkit.conf.env import (
    ENV_PREFIX_VARIABLE,
    PROJECT_ROOT_VARIABLE,
    project_prefix,
    project_root,
)
from domainkit.contrib.auth.principals import invalidate_all
from domainkit.contrib.messaging import tasks_kept
from domainkit.ddd import event_bus
from domainkit.http import get_asgi_application
from domainkit.http.metrics import refreshers_kept
from domainkit.log import reset_throttled
from domainkit.management import call_command
from domainkit.money import currencies_kept
from domainkit.reporting import flush_reports
from domainkit.testing import DatabaseBlocker
from domainkit_tests import engine_events  # noqa: F401 - its listeners first.
from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

type MakePackage = Callable[[str, dict[str, str]], Path]

POSTGRES_VARIABLE: Final = "DOMAINKIT_TEST_POSTGRES_URL"
"""A PostgreSQL server URL; when set, database tests run there (see database_url)."""
DATABASES_REFUSED: Final = DatabaseBlocker(
    "This test reached a database without requesting one. Request "
    "database_url, make_database, migrated, a project fixture (auth_project, "
    "api, identity), or database_access for the databases the settings name."
)
"""Refuses the databases outside the fixtures that make them, as the plugin
refuses a project's test databases to a test that did not request ``db``."""


MUTMUT_FAILURES: Final = Path("mutmut-failures.txt")
"""The test mutmut's own runs of the suite stopped at, in mutmut's copy.

mutmut runs its stats and its clean run in a forked child
(``process_isolation = "forkserver"``) and never shows that child's
output, so a failure there read only "failed to collect stats. runner
returned 1". CI's mutation jobs print this file then: the test that was
running, and what failed. A test that hangs until pytest-timeout ends the
process leaves only the first line.
"""


def _mutmut_checks_the_suite() -> bool:
    """Whether mutmut runs the suite unmutated: its stats, or its clean run."""
    return os.environ.get("MUTANT_UNDER_TEST", "-") in {"stats", ""}


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """Note the test mutmut's own run is at, in place of the last one."""
    if _mutmut_checks_the_suite():
        MUTMUT_FAILURES.write_text(f"running {nodeid}\n", encoding="utf-8")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Add what failed, when it fails in mutmut's own run."""
    if report.failed and _mutmut_checks_the_suite():
        with MUTMUT_FAILURES.open("a", encoding="utf-8") as noted:
            noted.write(
                f"failed {report.nodeid} ({report.when})\n{report.longreprtext}\n"
            )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the tutorial's parts in the order a reader reads them.

    They share one reader's walk (``test_walkthroughs.Tutorial``), so a
    shuffled order tests nothing more, and a late part run first plays
    every part before it. pytest-randomly shuffles first; the parts take
    the places it gave them, in page order.
    """
    places = [
        index
        for index, item in enumerate(items)
        if item.nodeid.startswith("domainkit_tests/test_walkthroughs.py::test_part_")
    ]
    parts = sorted((items[index] for index in places), key=lambda item: item.name)
    for index, item in zip(places, parts, strict=True):
        items[index] = item


@pytest.fixture(scope="session", autouse=True)
def databases_refused() -> Iterator[None]:
    """Refuse the databases to every test that requested none."""
    with DATABASES_REFUSED.block():
        yield


@pytest.fixture
def database_access() -> Iterator[None]:
    """Let the test reach the databases its settings name.

    For tests of the settings' own in-memory SQLite, of a database out of
    reach, or of engines and pools themselves.
    """
    with DATABASES_REFUSED.unblock():
        yield


CACHED: Final = type(functools.cache(len))
"""What ``functools.cache`` and ``lru_cache`` make of a function."""


def domainkit_caches() -> list[Any]:
    """Every ``functools`` cache of a loaded domainkit module, once each.

    Told apart by type: asking an object for ``cache_clear`` would load the
    lazy settings.
    """
    found: dict[int, Any] = {}
    for name, module in list(sys.modules.items()):
        if module is None or name.split(".", 1)[0] != "domainkit":
            continue
        for value in list(vars(module).values()):
            if type(value) is CACHED:
                found[id(value)] = value
    return list(found.values())


@pytest.fixture
def every_caller_seen_by_mutmut() -> None:
    """Under mutmut's stats run, start each test with domainkit's caches empty.

    That run records which tests call each function, and a mutant runs only
    those tests. A function behind a cache (a statement built once per
    table, a parsed address) runs for the first test that needs it, and
    with it every function it calls: the tests after it would be left out,
    and a mutant there would live whatever they check. Mutants of other
    runs keep their caches, filled by the mutated code itself.
    """
    forget_caches_under_mutmuts_stats()


def forget_caches_under_mutmuts_stats() -> int:
    """Empty domainkit's caches, in mutmut's stats run only; how many there are."""
    if os.environ.get("MUTANT_UNDER_TEST") != "stats":
        return 0
    cached = domainkit_caches()
    for each in cached:
        each.cache_clear()
    return len(cached)


@pytest.fixture(autouse=True)
def framework_settings(every_caller_seen_by_mutmut: None) -> Iterator[None]:
    """Run against ``domainkit_tests.settings`` with a fresh registry.

    After mutmut's stats run has emptied the caches: the settings are one.
    """
    registry.clear()
    AlphaConfig.calls.clear()
    AlphaConfig.healthy = True
    with use_settings("domainkit_tests.settings"):
        yield
    registry.clear()


@pytest.fixture(autouse=True)
def isolated_registries() -> Iterator[None]:
    """Restore process-wide registries (tasks, subscriptions, metrics) per test.

    Apps register tasks and handlers when they are installed; a test that
    installs one must not leak them into the next test. Memory caches and
    the principal cache start empty, too, and so do the throttled logs: an
    app's shutdown writes what every one of them held back, so a line held
    by an earlier test's app, still alive, would land in this test's log.
    """
    caches.reset()
    invalidate_all()
    reset_throttled()
    with registries_kept():
        yield


@contextmanager
def registries_kept() -> Iterator[None]:
    """Put the tasks, subscriptions, metric refreshers, and currencies back.

    Through their public resets, as the plugin does (TEST-05).
    """
    calls = list(AlphaConfig.calls)
    try:
        with (
            tasks_kept(),
            event_bus.subscriptions_kept(),
            refreshers_kept(),
            currencies_kept(),
        ):
            yield
    finally:
        AlphaConfig.calls[:] = calls


@pytest.fixture
def make_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MakePackage]:
    """Write throwaway packages under ``tmp_path`` and make them importable.

    ``make_package("pkg.sub", {"apps.py": "..."})`` creates every
    ``__init__.py`` on the way and returns the package directory. Imported
    modules are forgotten afterwards, so tests can reuse names.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    roots: set[str] = set()

    def make(name: str, files: dict[str, str]) -> Path:
        root = tmp_path.joinpath(*name.split("."))
        roots.add(name.partition(".")[0])
        for relative, content in {"__init__.py": "", **files}.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content), encoding="utf-8")
        for directory in [root, *root.parents]:
            if directory == tmp_path:
                break
            (directory / "__init__.py").touch()
        for directory in root.rglob("*"):
            if directory.is_dir() and directory.name != "__pycache__":
                (directory / "__init__.py").touch()
        importlib.invalidate_caches()
        return root

    yield make
    # Forget what came from here, by name or by place: a test may write more
    # packages under tmp_path (a generator's project) that later tests reuse.
    here = str(tmp_path)
    for name, module in list(sys.modules.items()):
        places = [getattr(module, "__file__", None), *getattr(module, "__path__", ())]
        if name.partition(".")[0] in roots or any(
            isinstance(place, str) and place.startswith(here) for place in places
        ):
            del sys.modules[name]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Make ``tmp_path`` the project root (for ``.env`` and ``pyproject.toml``)."""
    monkeypatch.setenv(PROJECT_ROOT_VARIABLE, str(tmp_path))
    monkeypatch.delenv(ENV_PREFIX_VARIABLE, raising=False)
    project_root.cache_clear()
    project_prefix.cache_clear()
    yield tmp_path
    project_root.cache_clear()
    project_prefix.cache_clear()


type MakeDatabase = Callable[..., str]


@pytest.fixture
def make_database(tmp_path: Path) -> Iterator[MakeDatabase]:
    """Create empty databases by name: SQLite files, or new PostgreSQL databases.

    Set ``DOMAINKIT_TEST_POSTGRES_URL`` to a server (CI does) to run the
    database tests there. Each database is new, and dropped afterwards;
    nothing else on that server is touched. The test may reach the
    databases (``database_access``).
    """
    server = os.environ.get(POSTGRES_VARIABLE)
    created: list[str] = []

    def make(name: str, template: str | None = None) -> str:
        if not server:
            return f"sqlite+aiosqlite:///{tmp_path / name}.sqlite3"
        database = f"domainkit_test_{uuid.uuid4().hex}"
        copying = "" if template is None else f' TEMPLATE "{template}"'
        on_server(server, f'CREATE DATABASE "{database}"{copying}')
        created.append(database)
        url = make_url(server).set(database=database)
        return url.render_as_string(hide_password=False)

    with DATABASES_REFUSED.unblock():
        yield make
    for database in created:
        assert server is not None
        on_server(server, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


@pytest.fixture
def database_url(make_database: MakeDatabase) -> str:
    """An empty database for one test (see ``make_database``)."""
    return make_database("test")


def on_server(url: str, statement: str) -> None:
    """Run one statement outside a transaction, in a thread with its own loop.

    Sync fixtures and tests call it, so it must not need the caller's loop.
    """

    async def run() -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql(statement)
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, run()).result()


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo ``dictConfig`` changes to the root logger, and end its threads."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def local_zone() -> Iterator[Callable[[str], None]]:
    """``local_zone("Asia/Tokyo")`` sets the local time zone until the test ends.

    ``TZ`` is the process's: the one place that changes it, so the original
    always comes back, failure or not. Under pytest-xdist each worker is a
    process of its own, so no other worker's test sees the change.
    """
    previous = os.environ.get("TZ")

    def use(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    try:
        yield use
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.fixture(autouse=True)
def reports_sent() -> Iterator[None]:
    """Send a test's error reports before the next test starts."""
    yield
    flush_reports()


CONTENTTYPES: Final = "domainkit.contrib.contenttypes"
AUTH: Final = "domainkit.contrib.auth"
SHOP: Final = "domainkit_tests.sample_domains.shop"
ALPHA: Final = "domainkit_tests.sample_domains.alpha"
FAST_HASHER: Final = "domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"


def domain_warnings(label: str) -> list[str]:
    """What domain ``label`` registered ``on_deployment_warnings`` to find."""
    return [found for hook in on_deployment_warnings.of(label) for found in hook.call()]


def domain_problems(label: str, app: Any) -> list[str]:  # noqa: ANN401 - any app.
    """What domain ``label`` registered ``on_check_application`` to find in ``app``."""
    return [
        found for hook in on_check_application.of(label) for found in hook.call(app)
    ]


async def start_domain(label: str) -> None:
    """Run domain ``label``'s ``on_startup`` hooks."""
    for hook in on_startup.of(label):
        await hook.call()


TEMPLATE_PREFIX: Final = "domainkit_template_"
"""PostgreSQL databases migrated once per run, which tests get copies of."""


@dataclass(frozen=True, slots=True)
class Shape:
    """The settings a fixture's project runs with, and its revisions' message.

    Every test of a shape starts from the same migrated database, so the
    shape is migrated once per run (``Templates``), not once per test.
    """

    name: str
    message: str
    settings: dict[str, Any]


@dataclass(slots=True)
class _Template:
    """A shape's revisions, and the database ``migrate`` left with them."""

    migrations: Path
    url: str
    database: str | None = None
    """The PostgreSQL database's name; None for a SQLite file."""
    path: Path | None = None
    """The SQLite file; None on PostgreSQL."""


class Templates:
    """Each shape migrated once per run (per pytest-xdist worker), then copied.

    The first test of a shape generates its revisions with ``makemigrations``
    and applies them with ``migrate`` to a template database; every test of
    that shape then gets a copy of the template: ``CREATE DATABASE ...
    TEMPLATE`` on PostgreSQL, a copy of the file on SQLite. Each test still
    starts from the database ``migrate`` left, in a database of its own,
    without paying for the migration.
    """

    def __init__(self, scratch: Path, server: str | None) -> None:
        """Keep templates under ``scratch``, or on the PostgreSQL ``server``."""
        self._scratch = scratch
        self._server = server
        self._made: dict[str, _Template] = {}

    def get(self, shape: Shape) -> _Template:
        """The shape's template, migrated on first use."""
        if (made := self._made.get(shape.name)) is None:
            with DATABASES_REFUSED.unblock():
                made = self._made[shape.name] = self._migrate(shape)
        return made

    def copy(self, shape: Shape, make_database: "MakeDatabase", target: Path) -> str:
        """A new database holding what the shape's template holds; its URL."""
        template = self.get(shape)
        if template.path is not None:
            shutil.copyfile(template.path, target)
            return f"sqlite+aiosqlite:///{target}"
        assert template.database is not None
        return make_database("test", template.database)

    def close(self) -> None:
        """Drop the PostgreSQL templates."""
        for template in self._made.values():
            if template.database is not None:
                assert self._server is not None
                on_server(
                    self._server,
                    f'DROP DATABASE IF EXISTS "{template.database}" WITH (FORCE)',
                )
        self._made.clear()

    def _migrate(self, shape: Shape) -> _Template:
        """Generate the shape's revisions and migrate a template database."""
        directory = self._scratch / shape.name
        directory.mkdir()
        if self._server:
            database = f"{TEMPLATE_PREFIX}{uuid.uuid4().hex}"
            on_server(self._server, f'CREATE DATABASE "{database}"')
            url = make_url(self._server).set(database=database)
            template = _Template(
                directory / "migrations",
                url.render_as_string(hide_password=False),
                database=database,
            )
        else:
            path = directory / "template.sqlite3"
            template = _Template(
                directory / "migrations", f"sqlite+aiosqlite:///{path}", path=path
            )
        registry.clear()
        try:
            with (
                registries_kept(),
                override_settings(
                    **shape.settings,
                    DATABASES={"default": {"URL": template.url}},
                    MIGRATIONS_DIR=template.migrations,
                ),
            ):
                setup()
                call_command(
                    "makemigrations", "-m", shape.message, stdout=io.StringIO()
                )
                call_command("migrate", stdout=io.StringIO())
        finally:
            registry.clear()
        return template


@pytest.fixture(scope="session")
def templates(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Templates]:
    """The run's migrated templates; PostgreSQL's are dropped when it ends."""
    made = Templates(
        tmp_path_factory.mktemp("templates"), os.environ.get(POSTGRES_VARIABLE)
    )
    try:
        yield made
    finally:
        made.close()


type Migrated = Callable[[Shape], AbstractContextManager[str]]


@pytest.fixture
def migrated(
    templates: Templates, make_database: "MakeDatabase", tmp_path: Path
) -> Migrated:
    """Open a shape's project in a migrated database of the test's own.

    ``with migrated(API) as url:`` sets the registry up for the shape's
    settings inside the block, and clears it after.
    """

    @contextmanager
    def use(shape: Shape) -> Iterator[str]:
        url = templates.copy(shape, make_database, tmp_path / "test.sqlite3")
        registry.clear()
        try:
            with override_settings(
                **shape.settings,
                DATABASES={"default": {"URL": url}},
                MIGRATIONS_DIR=templates.get(shape).migrations,
            ):
                setup()
                yield url
        finally:
            registry.clear()

    return use


@pytest.fixture
def auth_project(migrated: Migrated) -> Iterator[str]:
    """Content types, auth, and the shop domain, in a migrated database of its own.

    Revisions are generated and applied the way a project does it, so
    ``migrate`` also created the content types and permissions. Passwords
    use the fast test hasher.
    """
    with migrated(AUTH_PROJECT) as database_url:
        yield database_url


type UseAuth = Callable[..., None]


@pytest.fixture
def use_auth() -> Iterator[UseAuth]:
    """Install content types and auth with ``AUTH`` options, without a database.

    ``use_auth(PASSWORD_HASHERS=[...])``; other keyword arguments named in
    ``settings`` (upper case) override settings.
    """
    with ExitStack() as stack:

        def install(
            *, settings: dict[str, object] | None = None, **auth: object
        ) -> None:
            registry.clear()
            stack.enter_context(
                override_settings(
                    **{
                        "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, ALPHA],
                        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER], **auth},
                        **(settings or {}),
                    }
                )
            )
            setup()

        yield install
    registry.clear()


MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
    "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
]

type Api = Callable[..., TestClient]


@pytest.fixture
def api(migrated: Migrated) -> Iterator[Api]:
    """A migrated project with auth, audit, and idempotency; ``api(**AUTH)``."""
    auth = API.settings["AUTH"]
    with migrated(API), ExitStackClients() as clients:

        def make(
            *,
            client: tuple[str, int] = ("testclient", 50000),
            **options: Any,  # noqa: ANN401 - AUTH keys.
        ) -> TestClient:
            if options:
                clients.enter(override_settings(AUTH={**auth, **options}))
            return clients.client(client)

        yield make


class ExitStackClients:
    """Open clients and settings, closed in reverse order."""

    def __init__(self) -> None:
        """Start empty."""
        self._exits: list[Callable[[], object]] = []

    def __enter__(self) -> "ExitStackClients":
        """Nothing to start."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close what was opened, newest first."""
        while self._exits:
            self._exits.pop()()

    def enter(self, manager: Any) -> None:  # noqa: ANN401 - a context manager.
        """Enter ``manager`` until the end of the test."""
        manager.__enter__()
        self._exits.append(lambda: manager.__exit__(None, None, None))

    def client(self, address: tuple[str, int]) -> TestClient:
        """A client of a fresh application, from ``address``."""
        client = TestClient(get_asgi_application(), client=address)
        client.__enter__()
        self._exits.append(lambda: client.__exit__(None, None, None))
        return client


IDENTITY_DOMAINS: Final = [
    CONTENTTYPES,
    AUTH,
    "domainkit.contrib.audit",
    "domainkit.contrib.idempotency",
    "domainkit.contrib.messaging",
    "domainkit.contrib.mail",
    SHOP,
    ALPHA,
]
MEMORY_MAIL: Final = {"BACKEND": "domainkit.contrib.mail.backends.MemoryBackend"}
IDENTITY_AUTHENTICATION: Final = [
    "domainkit.contrib.auth.authentication.TokenAuthentication",
    "domainkit.contrib.auth.cookies.CookieAuthentication",
]


@pytest.fixture
def identity(migrated: Migrated) -> Iterator[Api]:
    """``api`` with messaging, and mail kept in memory: resets, signup, MFA.

    ``identity(**AUTH)`` returns a client; ``ROOT_ROUTER`` mounts the
    routers the identity tests need.
    """
    from domainkit.contrib import mail

    mail.outbox.clear()
    auth = IDENTITY.settings["AUTH"]
    with migrated(IDENTITY), ExitStackClients() as clients:

        def make(
            *,
            client: tuple[str, int] = ("testclient", 50000),
            **options: Any,  # noqa: ANN401 - AUTH keys.
        ) -> TestClient:
            if options:
                clients.enter(override_settings(AUTH={**auth, **options}))
            return clients.client(client)

        yield make
    mail.outbox.clear()


# Property tests in CI (GitHub sets CI): a failing example prints the
# @reproduce_failure blob that replays it, since a runner keeps no example
# database between runs; the seed alone needs the same code to replay.
hypothesis_settings.register_profile("ci", print_blob=True)
if os.environ.get("CI"):
    hypothesis_settings.load_profile("ci")


AUTH_PROJECT: Final = Shape(
    "auth_project",
    "auth",
    {
        "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, SHOP, ALPHA],
        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER]},
    },
)
API: Final = Shape(
    "api",
    "auth",
    {
        "INSTALLED_DOMAINS": [
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.audit",
            "domainkit.contrib.idempotency",
            SHOP,
            ALPHA,
        ],
        "ROOT_ROUTER": "domainkit_tests.sample_auth_routes.router",
        "MIDDLEWARE": MIDDLEWARE,
        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER]},
    },
)
IDENTITY: Final = Shape(
    "identity",
    "identity",
    {
        "INSTALLED_DOMAINS": IDENTITY_DOMAINS,
        "ROOT_ROUTER": "domainkit_tests.sample_identity_routes.router",
        "MIDDLEWARE": MIDDLEWARE,
        "MAIL": MEMORY_MAIL,
        "AUTH": {
            "PASSWORD_HASHERS": [FAST_HASHER],
            "AUTHENTICATION_CLASSES": IDENTITY_AUTHENTICATION,
        },
    },
)
