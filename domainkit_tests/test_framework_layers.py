"""The core imports no contrib app: apps build on the core, never the reverse.

The core is every module of ``domainkit`` outside ``domainkit.contrib``. It
may not import a contrib app, at the top of a module, inside a function,
or by name (``import_module("domainkit.contrib...")``): each would make the
core need an app a project may leave out (ADR 0080). What the core needs of
an app it asks through a protocol the app implements (``Principal``,
``OperatorRecorder``, ``QueueDeclarer``, ``TokenIssuer``).

``ALLOWED`` lists the only edges left, each with its reason; it may only
shrink, so an entry no longer needed fails too.
"""

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from domainkit_tests.conftest import MakePackage

SRC: Final = Path(__file__).parents[1] / "src"
LOADERS: Final = frozenset({"import_module", "import_string", "__import__"})
"""Functions that import a module named by a string."""

ALLOWED: Final = frozenset(
    {
        # For type checkers only: DomainConfig.RETENTION holds privacy's
        # Retention policies, until hooks are registered by type (DSGN-03).
        ("domainkit.apps.config", "domainkit.contrib.privacy", "typing"),
    }
)
"""``(core module, contrib module, how)`` edges that stay, and why."""


def edges(root: Path, package: str) -> Iterator[tuple[str, str, str]]:
    """``(module, imported, how)`` for each import of ``<package>.contrib`` by the core.

    ``how`` is ``eager`` (at import), ``lazy`` (in a function), ``typing``
    (under ``TYPE_CHECKING``), or ``by name`` (a loader given the name).
    """
    contrib = f"{package}.contrib"
    base = root.joinpath(*package.split("."))
    for path in sorted(base.rglob("*.py")):
        relative = path.relative_to(root).with_suffix("").parts
        if {"templates", "migration_env"} & set(relative):
            continue
        module = ".".join(relative[:-1] if relative[-1] == "__init__" else relative)
        if module == contrib or module.startswith(f"{contrib}."):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported, how in _imports(tree, module, path.name == "__init__.py"):
            if imported == contrib or imported.startswith(f"{contrib}."):
                yield module, imported, how


def _imports(
    tree: ast.Module, module: str, is_package: bool
) -> Iterator[tuple[str, str]]:
    """Every module ``tree`` imports, and how."""
    yield from _walk(tree.body, module, is_package, "eager")


def _walk(
    nodes: list[ast.stmt], module: str, is_package: bool, how: str
) -> Iterator[tuple[str, str]]:
    """The imports of ``nodes``, ``how`` as the enclosing code makes them."""
    for node in nodes:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                for alias in inner.names:
                    yield alias.name, _how(node, inner, how)
            elif isinstance(inner, ast.ImportFrom):
                base = _absolute(module, is_package, inner.level, inner.module)
                yield base, _how(node, inner, how)
            elif (
                isinstance(inner, ast.Call)
                and _called(inner) in LOADERS
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and isinstance(inner.args[0].value, str)
            ):
                yield inner.args[0].value, "by name"


def _how(top: ast.stmt, node: ast.AST, how: str) -> str:
    """``typing`` under ``if TYPE_CHECKING``, ``lazy`` in a function, else ``how``."""
    if (
        isinstance(top, ast.If)
        and _is_type_checking(top.test)
        and any(node is each for body in top.body for each in ast.walk(body))
    ):
        return "typing"
    for each in ast.walk(top):
        if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            node is inner for inner in ast.walk(each)
        ):
            return "lazy"
    return how


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


def test_the_core_imports_no_contrib_app() -> None:
    """Not at import, not in a function, not by name; ``ALLOWED`` only shrinks."""
    found = set(edges(SRC, "domainkit"))

    assert sorted(found - ALLOWED) == [], "Ask the app through a protocol instead."
    assert sorted(ALLOWED - found) == [], "No longer imported: remove from ALLOWED."


@pytest.mark.parametrize(
    ("source", "how"),
    [
        ("import fakekit.contrib.one\n", "eager"),
        ("from fakekit.contrib.one import models\n", "eager"),
        ("from ..contrib.one import models\n", "eager"),
        ("def f():\n    from fakekit.contrib.one import models\n", "lazy"),
        ("class C:\n    def f(self):\n        import fakekit.contrib.one\n", "lazy"),
        (
            (
                "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                "    from fakekit.contrib.one import models\n"
            ),
            "typing",
        ),
        (
            (
                "from importlib import import_module\n"
                "def f():\n    return import_module('fakekit.contrib.one.models')\n"
            ),
            "by name",
        ),
    ],
)
def test_the_check_finds_every_kind_of_edge(
    make_package: MakePackage, tmp_path: Path, source: str, how: str
) -> None:
    """A new edge of each kind fails the rule; the apps' own imports do not."""
    make_package(
        "fakekit.contrib.one",
        {"__init__.py": "", "models.py": "from fakekit.core import rules\n"},
    )
    make_package("fakekit.core", {"rules.py": source})

    found = set(edges(tmp_path, "fakekit"))

    assert {(module, kind) for module, _, kind in found} == {
        ("fakekit.core.rules", how)
    }
    assert all(imported.startswith("fakekit.contrib.one") for _, imported, _ in found)


def test_the_check_lets_the_core_import_the_core(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """Imports of the core, the standard library, and names that only mention apps."""
    make_package(
        "fakekit.core",
        {
            "rules.py": (
                "import json\nfrom fakekit.core import other\n"
                "NAME = 'fakekit.contrib.one'\n"
                "def f(registry):\n    return registry.is_installed(NAME)\n"
            ),
            "other.py": "",
        },
    )

    assert set(edges(tmp_path, "fakekit")) == set()
