"""Helpers shared between test files live in ``domainkit_tests/support``.

A test file that imports a helper from another test file is tied to it:
neither can be moved or renamed alone, and the helper is found only by
knowing which file used it first. ``support`` holds what several files
need, by area.
"""

import ast
from pathlib import Path
from typing import Final

TESTS: Final = Path(__file__).parent
SUPPORT: Final = TESTS / "support"


def imported(path: Path) -> list[tuple[str, str]]:
    """``(module, name)`` for each ``from module import name`` in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.module or "", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]


def test_support_imports_no_test_file() -> None:
    """What every test file may use depends on none of them."""
    for path in sorted(SUPPORT.glob("*.py")):
        from_tests = [
            module
            for module, _ in imported(path)
            if module.startswith("domainkit_tests.test_")
        ]
        assert from_tests == [], path.name


def test_a_helper_in_support_is_imported_from_support() -> None:
    """No test file takes a helper of ``support`` through another test file."""
    passed_on = {
        path.stem: {
            name
            for module, name in imported(path)
            if module.startswith("domainkit_tests.support")
        }
        for path in sorted(TESTS.glob("test_*.py"))
    }
    assert "create_user" in passed_on["test_auth_http"]

    for path in sorted(TESTS.glob("test_*.py")):
        for module, name in imported(path):
            source = module.removeprefix("domainkit_tests.")
            assert name not in passed_on.get(source, ()), (path.name, module, name)
