"""Every problem code domainkit answers with is listed, pinned, and listed once.

Clients switch on an error's ``code`` (and, with problem details, on its
``type``, which ends in the code), so codes are public: ``docs/reference/
problems.md`` lists each one with its status, and this test fails when the
framework can answer a code the page does not list, the page lists one it
no longer answers, or a status changes (DSGN-20, WEB-25).

The codes are found where the framework writes them:

- the ``code`` of each error class domainkit defines (``DomainError``,
  ``ServiceBusy``, ``MoneyError``, ``UpstreamError``), which must be set on
  the class itself, never derived from its name or inherited;
- every ``code=`` the source passes as a literal or a module constant
  (``APIError``, ``error_response``, a batch item's result), and the default
  of every ``code`` parameter;
- the codes Starlette's and FastAPI's own errors get (``default_code``).
"""

import ast
import importlib
import inspect
import re
import textwrap
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from domainkit.ddd.exceptions import DomainError
from domainkit.exceptions import ServiceBusy, UpstreamError
from domainkit.http.errors import default_code, status_code_for
from domainkit.money import MoneyError

ROOT: Final = Path(__file__).parents[1]
SRC: Final = ROOT / "src" / "domainkit"
PAGE: Final = ROOT / "docs" / "reference" / "problems.md"
ROW: Final = re.compile(r"^\| `([a-z0-9_]+)` \| ([0-9, ]+) \|")
SKIPPED: Final = frozenset({"templates", "migration_env", "static", "locale"})
"""Directories of the source that hold no module the framework imports as is."""
BY_STARLETTE: Final = (400, 404, 405)
"""Statuses Starlette's and FastAPI's own errors answer with (``default_code``):
an unparsable body, an unknown path, a method the path does not take."""
ROOTS: Final = (DomainError, ServiceBusy, MoneyError, UpstreamError)
STATUS_NAME: Final = re.compile(r"HTTP_([0-9]{3})_")

pytestmark = pytest.mark.reads_own_source

type Codes = dict[str, set[int]]


def modules() -> Iterator[tuple[str, Path]]:
    """Every module of the source, by dotted name."""
    for path in sorted(SRC.rglob("*.py")):
        if SKIPPED & set(path.relative_to(SRC).parts):
            continue
        parts = path.relative_to(SRC.parent).with_suffix("").parts
        yield ".".join(parts[:-1] if parts[-1] == "__init__" else parts), path


def status_of(node: ast.expr | None) -> int | None:
    """The status an argument spells: ``404``, or ``status.HTTP_404_NOT_FOUND``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    name = node.attr if isinstance(node, ast.Attribute) else None
    match = STATUS_NAME.match(name or "")
    return int(match[1]) if match else None


def written(tree: ast.Module) -> Iterator[tuple[str, int | None]]:
    """The codes a module passes as ``code=`` or defaults a ``code`` to.

    A code is a literal, or a constant the module defines (``OVERLOADED``);
    its status is the call's first argument where it spells one.
    """
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }

    def code(value: ast.expr) -> str | None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name):
            return constants.get(value.id)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "code" and (found := code(keyword.value)):
                    given = {each.arg: each.value for each in node.keywords}
                    first = node.args[0] if node.args else None
                    yield found, status_of(given.get("status_code", first))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            positional = [*arguments.posonlyargs, *arguments.args]
            named = [*positional, *arguments.kwonlyargs]
            defaults = [
                *([None] * (len(positional) - len(arguments.defaults))),
                *arguments.defaults,
                *arguments.kw_defaults,
            ]
            for argument, default in zip(named, defaults, strict=True):
                found = None if default is None else code(default)
                if argument.arg == "code" and found:
                    yield found, None


def subclasses(root: type[Any]) -> Iterator[type[Any]]:
    """``root`` and every class below it."""
    yield root
    children: list[type[Any]] = root.__subclasses__()
    for each in children:
        yield from subclasses(each)


def class_status(error: type) -> int:
    """The status the HTTP layer answers an instance of ``error`` with."""
    if issubclass(error, DomainError):
        return status_code_for(error.__new__(error))
    if issubclass(error, UpstreamError):
        return error.status_code
    return 503 if issubclass(error, ServiceBusy) else 422


def sets_code(error: type) -> bool:
    """Whether ``error``'s own body assigns ``code``."""
    body = ast.parse(textwrap.dedent(inspect.getsource(error))).body[0]
    assert isinstance(body, ast.ClassDef)
    return any(
        isinstance(target, ast.Name) and target.id == "code"
        for node in body.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    )


def registers_tasks(name: str) -> bool:
    """Whether ``name`` is a contrib app's tasks module: it registers on import.

    It needs its app installed, and defines no class (checked below).
    """
    return name.startswith("domainkit.contrib.") and name.endswith(".tasks")


@pytest.fixture(scope="module")
def classes() -> list[type[Any]]:
    """Every error class domainkit defines, its modules imported."""
    for name, path in modules():
        if registers_tasks(name):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assert not any(isinstance(node, ast.ClassDef) for node in tree.body)
            continue
        importlib.import_module(name)
    return [
        error
        for root in ROOTS
        for error in subclasses(root)
        if error.__module__.startswith("domainkit.")
    ]


@pytest.fixture(scope="module")
def answered(classes: list[type[Any]]) -> Codes:
    """Every code the framework answers with, and its statuses where known."""
    codes: Codes = defaultdict(set)
    for error in classes:
        codes[error.code].add(class_status(error))
    for _, path in modules():
        for found, status in written(ast.parse(path.read_text(encoding="utf-8"))):
            codes[found].update([] if status is None else [status])
    for status in BY_STARLETTE:
        codes[default_code(status)].add(status)
    return dict(codes)


def listed() -> Codes:
    """The codes the page lists, and their statuses."""
    codes: Codes = {}
    for number, line in enumerate(PAGE.read_text(encoding="utf-8").splitlines(), 1):
        if match := ROW.match(line):
            assert match[1] not in codes, f"line {number}: {match[1]} again"
            codes[match[1]] = {int(each) for each in match[2].split(",")}
    return codes


def test_every_error_class_sets_its_own_code(classes: list[type[Any]]) -> None:
    """None derives its code from its name, or inherits its base's."""
    unpinned = sorted(
        f"{error.__module__}.{error.__qualname__}"
        for error in classes
        if not sets_code(error)
    )
    assert unpinned == []


def test_the_page_lists_every_code_and_its_statuses(answered: Codes) -> None:
    """A code answered and not listed, or listed and not answered, fails."""
    page = listed()

    assert sorted(set(answered) - set(page)) == [], "answered, not listed"
    assert sorted(set(page) - set(answered)) == [], "listed, not answered"
    differ = {
        code: (sorted(page[code]), sorted(statuses))
        for code, statuses in answered.items()
        if statuses and not statuses <= page[code]
    }
    assert differ == {}, "statuses the page does not list"


def test_one_code_answers_every_rate_limit(answered: Codes) -> None:
    """Every 429 is ``rate_limited``: a client tells limits apart by nothing else."""
    assert sorted(code for code, statuses in answered.items() if 429 in statuses) == [
        "rate_limited"
    ]
