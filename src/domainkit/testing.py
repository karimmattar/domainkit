"""pytest plugin for domainkit projects: test settings, a database, a client.

Enable it, and name the test settings, in the project's ``pyproject.toml``::

    [tool.pytest.ini_options]
    addopts = "-p domainkit.testing"
    domainkit_settings = "config.settings.test"

The ``.env`` file is never read during tests. The fixtures:

``database``
    Session-wide: migrates the test database up, down to base, and up again,
    which proves every revision is reversible and empties a reused database.
    An in-memory SQLite URL becomes a temporary file, so every connection
    sees the same tables. Yields the URL.
``client``
    A ``TestClient`` for the project's application, lifespan included, on
    the migrated database. Requests send no credentials.
``client_for``
    ``client_for("shop.view_product", ...)``: a client of the same
    application that authenticates as a new user granted those
    permissions. Keyword arguments set the user's fields
    (``is_staff=True``); the username is unique unless given. Needs
    ``domainkit.contrib.auth``.
``admin_client``
    A client that authenticates as a new superuser. The tests
    ``startdomain`` generates use it.

Memory caches (``memory://``) are emptied before every test, so no test
sees what another one cached. Redis caches are left alone.
"""

import io
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from domainkit.cache import caches
from domainkit.conf import DEFAULT_DB_ALIAS, DatabaseConfig, override_settings, settings
from domainkit.conf.env import project_prefix
from domainkit.http import get_asgi_application
from domainkit.management import call_command
from domainkit.management.base import run_async

IN_MEMORY: Final = "sqlite+aiosqlite:///:memory:"
SETTINGS_INI: Final = "domainkit_settings"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Declare the ``domainkit_settings`` ini option."""
    parser.addini(SETTINGS_INI, "Settings module for the tests (config.settings.test)")


def pytest_configure(config: pytest.Config) -> None:
    """Select the test settings and keep the developer's ``.env`` out."""
    prefix = project_prefix()
    if module := config.getini(SETTINGS_INI):
        os.environ[f"{prefix}SETTINGS_MODULE"] = str(module)
    os.environ[f"{prefix}ENV_FILE"] = ""


@contextmanager
def migrated_database(scratch: Path) -> Iterator[str]:
    """Migrate every target up, down, and up; yield the default database's URL.

    Each in-memory SQLite database becomes a file in ``scratch``
    (``test.sqlite3`` for ``default``, ``test_<alias>.sqlite3`` for others).
    """
    databases: dict[str, DatabaseConfig] = {}
    for alias, config in settings.DATABASES.items():
        databases[alias] = config
        if config.URL.get_secret_value() == IN_MEMORY:
            name = "test" if alias == DEFAULT_DB_ALIAS else f"test_{alias}"
            url = SecretStr(f"sqlite+aiosqlite:///{scratch / name}.sqlite3")
            databases[alias] = config.model_copy(update={"URL": url})
    output = io.StringIO()
    with override_settings(DATABASES=databases):
        call_command("migrate", stdout=output)
        call_command("migrate", "base", downgrade=True, stdout=output)
        call_command("migrate", stdout=output)
        yield settings.DATABASES[DEFAULT_DB_ALIAS].URL.get_secret_value()


@pytest.fixture(autouse=True)
def empty_memory_caches() -> None:
    """Start every test with empty memory caches."""
    caches.reset()


@pytest.fixture(scope="session")
def database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The migrated test database's URL."""
    with migrated_database(tmp_path_factory.mktemp("database")) as url:
        yield url


@pytest.fixture
def client(database: str) -> Iterator[TestClient]:
    """Serve the project's application on the test database."""
    with TestClient(get_asgi_application()) as test_client:
        yield test_client


type ClientFor = Callable[..., TestClient]


@pytest.fixture
def client_for(client: TestClient) -> ClientFor:
    """Make clients that authenticate as new users with permissions."""

    def make(
        *permissions: str,
        **fields: Any,  # noqa: ANN401 - the user model's fields.
    ) -> TestClient:
        token = run_async(_user_token(permissions, fields))
        return TestClient(client.app, headers={"Authorization": f"Bearer {token}"})

    return make


@pytest.fixture
def admin_client(client_for: ClientFor) -> TestClient:
    """A client that authenticates as a new superuser."""
    return client_for(is_superuser=True)


async def _user_token(permissions: tuple[str, ...], fields: dict[str, Any]) -> str:
    """Store a user with ``permissions``; return an access token for them.

    The token comes from the first ``AUTH.AUTHENTICATION_CLASSES`` class,
    as a login's would.
    """
    from domainkit.contrib.auth import AuthUnitOfWork, get_user_model
    from domainkit.contrib.auth.authentication import get_authenticators

    username = fields.pop(get_user_model().USERNAME_FIELD, f"user-{uuid4().hex}")
    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user(username, **fields)
        user.grant(*await uow.permissions.get_many(permissions))
        issued = await get_authenticators()[0].issue(uow, user)
        await uow.commit()
    return issued.access_token
