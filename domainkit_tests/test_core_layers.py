"""The core's packages import their own layer and the layers below, never above.

ADR 0188 writes the layering down. From the bottom:

0. Foundations: errors, helpers, the clock, deadlines, and task tracking.
1. Settings and the model: settings, the domain model and money, apps,
   languages, signing, the request context, logs, and error reports.
2. Adapters: databases, caches, storages, keys, clouds, brokers, ledgers,
   search, documents, and the checks of uploaded files.
3. Serving: HTTP, resources, tenancy, the debug pages, and the process's
   start and stop.
4. Tools: commands, the pytest plugin, and the layer checker.

The contrib apps sit above them all (``test_framework_layers.py``). A
package is ``domainkit.<name>``, a module of the root (``domainkit.clock``)
or a directory (``domainkit.db``); ``domainkit`` itself is the root
package, which ``setup()`` makes a layer 1 package.

Every import counts: at the top of a module, in a function, under
``TYPE_CHECKING``, or by a loader given the module's name, as the project
layer rules count them (``domainkit.architecture``). ``ALLOWED`` lists the
upward imports 2.8 had, each with its reason. It may only shrink: a new
one fails, and so does an entry no longer needed.
"""

import ast
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import pytest

from domainkit_tests.conftest import MakePackage
from domainkit_tests.test_framework_layers import LOADERS

SRC: Final = Path(__file__).parents[1] / "src"

LAYERS: Final = (
    frozenset(
        {
            "checks",
            "clock",
            "deadlines",
            "deprecation",
            "exceptions",
            "tasks",
            "tracing",
            "utils",
        }
    ),
    frozenset(
        {
            "domainkit",
            "apps",
            "conf",
            "context",
            "ddd",
            "encryption",
            "i18n",
            "log",
            "money",
            "reporting",
            "signing",
        }
    ),
    frozenset(
        {
            "broker",
            "cache",
            "cloud",
            "db",
            "documents",
            "inspection",
            "keys",
            "ledger",
            "search",
            "storage",
        }
    ),
    frozenset({"debug", "http", "lifecycle", "resources", "tenancy"}),
    frozenset({"__main__", "architecture", "management", "testing"}),
)
"""The core's packages, by layer, from the bottom (ADR 0188)."""

CLIENT: Final = "domainkit.http.client"
ALLOWED: Final = frozenset(
    {
        # The outgoing HTTP client sits in the serving package (ADR 0050).
        # Every adapter that calls a service imports it; it moves to a
        # package of its own below the adapters in a later release.
        ("domainkit.broker.sqs", CLIENT, "lazy"),
        ("domainkit.cloud.aws", CLIENT, "lazy"),
        ("domainkit.cloud.aws", CLIENT, "typing"),
        ("domainkit.cloud.azure", CLIENT, "lazy"),
        ("domainkit.cloud.google", CLIENT, "lazy"),
        ("domainkit.documents.gotenberg", CLIENT, "eager"),
        ("domainkit.keys.aws", CLIENT, "lazy"),
        ("domainkit.keys.azure", CLIENT, "lazy"),
        ("domainkit.keys.gcp", CLIENT, "lazy"),
        ("domainkit.keys.huawei", CLIENT, "lazy"),
        ("domainkit.keys.vault", CLIENT, "lazy"),
        ("domainkit.search._http", CLIENT, "lazy"),  # Both engines' shared client.
        ("domainkit.search._http", CLIENT, "typing"),
        ("domainkit.storage.azure", CLIENT, "lazy"),
        ("domainkit.storage.gcs", CLIENT, "lazy"),
        ("domainkit.storage.s3", CLIENT, "lazy"),
        # For type checkers only: DomainConfig.databases(router) answers
        # where the domain's data lives (ADR 0101), so it names the router.
        ("domainkit.apps.config", "domainkit.db.routing", "typing"),
        # The settings validate ARCHITECTURE.EXTRA_LAYERS against the
        # built-in layer names, which the layer checker defines.
        ("domainkit.conf.global_settings", "domainkit.architecture", "eager"),
        # The settings make the default virus scanner when asked for it.
        ("domainkit.conf.global_settings", "domainkit.storage.scanning", "lazy"),
        # A tenant's schema is named by tenancy's rule when its tables are
        # migrated, or its work runs there.
        ("domainkit.db.migrations", "domainkit.tenancy", "lazy"),
        ("domainkit.db.uow", "domainkit.tenancy", "lazy"),
    }
)
"""``(importer, imported, how)`` edges up the layers that stay, and why."""


class Core:
    """The modules of one framework-shaped package, and its packages' layers."""

    def __init__(
        self, root: Path, package: str, layers: tuple[frozenset[str], ...]
    ) -> None:
        """Index every module of ``package`` under ``root`` but its apps."""
        self.package = package
        self.files: dict[str, Path] = {}
        base = root.joinpath(*package.split("."))
        for path in sorted(base.rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            if {"templates", "migration_env"} & set(parts):
                continue
            if parts[-1] == "__init__":
                parts = parts[:-1]
            module = ".".join(parts)
            if self.area(module) != "contrib":
                self.files[module] = path
        self.layer: Mapping[str, int] = {
            name: number for number, names in enumerate(layers) for name in names
        }

    def area(self, module: str) -> str:
        """The core package ``module`` is in: ``db`` for ``<package>.db.uow``."""
        parts = module.split(".")
        prefix = self.package.split(".")
        return parts[len(prefix)] if len(parts) > len(prefix) else parts[-1]

    def areas(self) -> set[str]:
        """Every core package there is."""
        return {self.area(module) for module in self.files}

    def module_of(self, name: str) -> str | None:
        """The longest core module ``name`` is, or is inside."""
        parts = name.split(".")
        while parts:
            if (candidate := ".".join(parts)) in self.files:
                return candidate
            parts.pop()
        return None

    def imports(self) -> Iterator[tuple[str, str, str]]:
        """``(importer, imported, how)`` for each import of the core by the core."""
        for importer, path in self.files.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            is_package = path.name == "__init__.py"
            for name, how in _walk(tree.body, importer, is_package, "eager"):
                imported = self.module_of(name)
                if imported is not None and imported != importer:
                    yield importer, imported, how

    def upward(self) -> set[tuple[str, str, str]]:
        """The imports of a package in a higher layer than the importer's."""
        return {
            (importer, imported, how)
            for importer, imported, how in self.imports()
            if self.layer[self.area(imported)] > self.layer[self.area(importer)]
        }


def _walk(
    nodes: list[ast.stmt], module: str, is_package: bool, how: str
) -> Iterator[tuple[str, str]]:
    """``(name, how)`` for each import ``nodes`` make, with each name imported."""
    for node in nodes:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            yield from _walk(node.body, module, is_package, "typing")
            yield from _walk(node.orelse, module, is_package, how)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            inner = "typing" if how == "typing" else "lazy"
            yield from _walk(node.body, module, is_package, inner)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, how
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(module, is_package, node.level, node.module)
            for alias in node.names:
                yield f"{base}.{alias.name}", how
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    yield from _walk([child], module, is_package, how)
                elif isinstance(child, ast.ExceptHandler | ast.match_case):
                    yield from _walk(child.body, module, is_package, how)
            yield from _by_name(node, how)


def _by_name(node: ast.stmt, how: str) -> Iterator[tuple[str, str]]:
    """The modules a loader is given by name in ``node``'s own expressions."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt | ast.excepthandler | ast.match_case):
            continue
        for each in ast.walk(child):
            if (
                isinstance(each, ast.Call)
                and _called(each) in LOADERS
                and each.args
                and isinstance(each.args[0], ast.Constant)
                and isinstance(each.args[0].value, str)
            ):
                yield each.args[0].value, how


def _is_type_checking(test: ast.expr) -> bool:
    """Whether ``test`` is ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _called(call: ast.Call) -> str | None:
    """The name of the function ``call`` calls, if it is a plain or dotted name."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _absolute(module: str, is_package: bool, level: int, target: str | None) -> str:
    """The absolute module a ``from`` import of ``module`` names."""
    if level == 0:
        return target or ""
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (level - 1)]
    return ".".join([*base, target] if target else base)


def test_every_core_package_has_one_layer() -> None:
    """A new package says where it sits; a layer names no package that is gone."""
    core = Core(SRC, "domainkit", LAYERS)
    named = [name for names in LAYERS for name in names]

    assert sorted(core.areas() - set(named)) == [], "Give it a layer in LAYERS."
    assert sorted(set(named) - core.areas()) == [], "No such package: remove it."
    assert len(named) == len(set(named))


def test_the_core_imports_no_layer_above_its_own() -> None:
    """However the import is made; the list of edges left only shrinks."""
    found = Core(SRC, "domainkit", LAYERS).upward()

    assert sorted(found - ALLOWED) == [], "Import downward, or ask through a hook."
    assert sorted(ALLOWED - found) == [], "No longer imported: remove from ALLOWED."


FAKE_LAYERS: Final = (
    frozenset({"fakekit", "base"}),
    frozenset({"db"}),
    frozenset({"http"}),
)


@pytest.mark.parametrize(
    ("source", "how"),
    [
        ("import fakekit.http.client\n", "eager"),
        ("from fakekit.http.client import Client\n", "eager"),
        ("from ..http import client\n", "eager"),
        ("from fakekit import http\n", "eager"),
        ("def f():\n    from fakekit.http import client\n", "lazy"),
        ("class C:\n    def f(self):\n        import fakekit.http\n", "lazy"),
        ("try:\n    import fakekit.http\nexcept ImportError:\n    pass\n", "eager"),
        (
            (
                "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                "    from fakekit.http.client import Client\n"
            ),
            "typing",
        ),
        (
            (
                "from importlib import import_module\n"
                "def f():\n    return import_module('fakekit.http.client')\n"
            ),
            "lazy",
        ),
        (
            "from importlib import import_module\nimport_module('fakekit.http')\n",
            "eager",
        ),
    ],
)
def test_the_check_finds_every_kind_of_upward_import(
    make_package: MakePackage, tmp_path: Path, source: str, how: str
) -> None:
    """An import of a higher layer fails, however it is made."""
    make_package("fakekit.http", {"__init__.py": "", "client.py": "class Client: ..."})
    make_package("fakekit.db", {"__init__.py": "", "uow.py": source})

    found = Core(tmp_path, "fakekit", FAKE_LAYERS).upward()

    assert {(importer, kind) for importer, _, kind in found} == {
        ("fakekit.db.uow", how)
    }
    assert all(imported.startswith("fakekit.http") for _, imported, _ in found)


def test_the_check_lets_a_layer_import_itself_and_those_below(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """Down the layers, within one, the root, the apps, and other libraries pass."""
    make_package("fakekit.base", {"__init__.py": "", "text.py": "import json\n"})
    make_package(
        "fakekit.db",
        {
            "__init__.py": "from .uow import Work\n",
            "uow.py": """
                from fakekit.base import text
                from .. import base
                class Work: ...
            """,
        },
    )
    make_package(
        "fakekit.http",
        {"__init__.py": "from fakekit.db import Work\nimport fakekit\n"},
    )
    make_package("fakekit.contrib.app", {"__init__.py": "from fakekit.http import x\n"})

    core = Core(tmp_path, "fakekit", FAKE_LAYERS)

    assert core.upward() == set()
    assert core.areas() == {"fakekit", "base", "db", "http"}
