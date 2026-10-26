"""Fixtures for framework tests: isolated settings, registry, and packages."""

import asyncio
import importlib
import io
import logging
import os
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from hypothesis import settings as hypothesis_settings
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit import setup
from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.conf import override_settings, use_settings
from domainkit.conf.env import (
    ENV_PREFIX_VARIABLE,
    PROJECT_ROOT_VARIABLE,
    project_prefix,
    project_root,
)
from domainkit.contrib.auth.principals import invalidate_all
from domainkit.contrib.messaging import registry as task_registry
from domainkit.ddd import event_bus
from domainkit.http import get_asgi_application, metrics
from domainkit.management import call_command
from domainkit.reporting import flush_reports
from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

type MakePackage = Callable[[str, dict[str, str]], Path]

POSTGRES_VARIABLE: Final = "DOMAINKIT_TEST_POSTGRES_URL"
"""A PostgreSQL server URL; when set, database tests run there (see database_url)."""


@pytest.fixture(autouse=True)
def framework_settings() -> Iterator[None]:
    """Run against ``domainkit_tests.settings`` with a fresh registry."""
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
    the principal cache start empty, too.
    """
    caches.reset()
    invalidate_all()
    tasks = dict(task_registry._tasks)
    subscriptions = {key: list(value) for key, value in event_bus._handlers.items()}
    refreshers = list(metrics._refreshers)
    yield
    task_registry._tasks.clear()
    task_registry._tasks.update(tasks)
    event_bus._handlers.clear()
    event_bus._handlers.update(subscriptions)
    metrics._refreshers[:] = refreshers


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


type MakeDatabase = Callable[[str], str]


@pytest.fixture
def make_database(tmp_path: Path) -> Iterator[MakeDatabase]:
    """Create empty databases by name: SQLite files, or new PostgreSQL databases.

    Set ``DOMAINKIT_TEST_POSTGRES_URL`` to a server (CI does) to run the
    database tests there. Each database is new, and dropped afterwards;
    nothing else on that server is touched.
    """
    server = os.environ.get(POSTGRES_VARIABLE)
    created: list[str] = []

    def make(name: str) -> str:
        if not server:
            return f"sqlite+aiosqlite:///{tmp_path / name}.sqlite3"
        database = f"domainkit_test_{uuid.uuid4().hex}"
        on_server(server, f'CREATE DATABASE "{database}"')
        created.append(database)
        url = make_url(server).set(database=database)
        return url.render_as_string(hide_password=False)

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


@pytest.fixture
def auth_project(database_url: str, tmp_path: Path) -> Iterator[str]:
    """Content types, auth, and the shop domain, migrated into an empty database.

    Revisions are generated and applied the way a project does it, so
    ``migrate`` also creates the content types and permissions. Passwords
    use the fast test hasher.
    """
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, SHOP, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        call_command("makemigrations", "-m", "auth", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield database_url
    registry.clear()


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
def api(database_url: str, tmp_path: Path) -> Iterator[Api]:
    """A migrated project with auth, audit, and idempotency; ``api(**AUTH)``."""
    registry.clear()
    auth: dict[str, Any] = {"PASSWORD_HASHERS": [FAST_HASHER]}
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.audit",
            "domainkit.contrib.idempotency",
            SHOP,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE,
        AUTH=auth,
    ):
        setup()
        call_command("makemigrations", "-m", "auth", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with ExitStackClients() as clients:

            def make(
                *,
                client: tuple[str, int] = ("testclient", 50000),
                **options: Any,  # noqa: ANN401 - AUTH keys.
            ) -> TestClient:
                if options:
                    clients.enter(override_settings(AUTH={**auth, **options}))
                return clients.client(client)

            yield make
    registry.clear()


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
def identity(database_url: str, tmp_path: Path) -> Iterator[Api]:
    """``api`` with messaging, and mail kept in memory: resets, signup, MFA.

    ``identity(**AUTH)`` returns a client; ``ROOT_ROUTER`` mounts the
    routers the identity tests need.
    """
    from domainkit.contrib import mail

    registry.clear()
    mail.outbox.clear()
    auth: dict[str, Any] = {
        "PASSWORD_HASHERS": [FAST_HASHER],
        "AUTHENTICATION_CLASSES": IDENTITY_AUTHENTICATION,
    }
    with override_settings(
        INSTALLED_DOMAINS=IDENTITY_DOMAINS,
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER="domainkit_tests.sample_identity_routes.router",
        MIDDLEWARE=MIDDLEWARE,
        MAIL=MEMORY_MAIL,
        AUTH=auth,
    ):
        setup()
        call_command("makemigrations", "-m", "identity", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with ExitStackClients() as clients:

            def make(
                *,
                client: tuple[str, int] = ("testclient", 50000),
                **options: Any,  # noqa: ANN401 - AUTH keys.
            ) -> TestClient:
                if options:
                    clients.enter(override_settings(AUTH={**auth, **options}))
                return clients.client(client)

            yield make
    registry.clear()
    mail.outbox.clear()


# Property tests in CI (GitHub sets CI): a failing example prints the
# @reproduce_failure blob that replays it, since a runner keeps no example
# database between runs; the seed alone needs the same code to replay.
hypothesis_settings.register_profile("ci", print_blob=True)
if os.environ.get("CI"):
    hypothesis_settings.load_profile("ci")
