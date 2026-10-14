"""Fixtures for framework tests: isolated settings, registry, and packages."""

import asyncio
import importlib
import logging
import os
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit.apps import registry
from domainkit.conf import use_settings
from domainkit.conf.env import (
    ENV_PREFIX_VARIABLE,
    PROJECT_ROOT_VARIABLE,
    project_prefix,
    project_root,
)
from domainkit.contrib.messaging import registry as task_registry
from domainkit.ddd import event_bus
from domainkit.http import metrics
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
    installs one must not leak them into the next test.
    """
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
    for module in list(sys.modules):
        if module.partition(".")[0] in roots:
            del sys.modules[module]


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


@pytest.fixture
def database_url(tmp_path: Path) -> Iterator[str]:
    """An empty database for one test: a SQLite file, or a new PostgreSQL database.

    Set ``DOMAINKIT_TEST_POSTGRES_URL`` to a server (CI does) to run the
    database tests there. Each test then gets a database of its own, dropped
    afterwards; nothing else on that server is touched.
    """
    server = os.environ.get(POSTGRES_VARIABLE)
    if not server:
        yield f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}"
        return
    name = f"domainkit_test_{uuid.uuid4().hex}"
    on_server(server, f'CREATE DATABASE "{name}"')
    try:
        yield make_url(server).set(database=name).render_as_string(hide_password=False)
    finally:
        on_server(server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


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
    """Undo ``dictConfig`` changes to the root logger."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
