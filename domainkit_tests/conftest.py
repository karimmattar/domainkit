"""Fixtures for framework tests: isolated settings, registry, and packages."""

import importlib
import logging
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

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
def restore_logging() -> Iterator[None]:
    """Undo ``dictConfig`` changes to the root logger."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
