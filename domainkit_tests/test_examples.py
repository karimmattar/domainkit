"""The runnable examples (``examples/``) work with the framework as it is now.

Each example is a project that ``domainkit init`` made, then edited to
solve one problem (ADR 0190). Its test here plays the steps of its own CI
workflow, as test_init_fintech.py plays the fintech profile's: ruff, mypy,
``check``, ``migrate``, ``makemigrations --check`` and its tests, in a copy
of it, with this domainkit and none of our settings. An example holds no
migrations: they are made for the domainkit installed, as its README's
first steps make them. With ``DOMAINKIT_TEST_POSTGRES_URL`` set, its tests
run again on PostgreSQL.
"""

import ast
import os
import re
import shlex
import shutil
import textwrap
import tomllib
from fnmatch import fnmatch
from importlib.util import find_spec
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from packaging.requirements import Requirement
from sqlalchemy import make_url

from domainkit_tests.conftest import on_server
from domainkit_tests.test_init import ROOT
from domainkit_tests.test_init_fintech import Project

EXAMPLES = ROOT / "examples"
PASSED = {
    "payments-ledger": ("LEDGER_API_", 14),
    "maker-checker": ("PAYOUTS_API_", 17),
    "outbox-webhooks": ("TRANSFERS_API_", 13),
}
"""Each example, its settings' prefix, and how many of its tests pass."""
PLANNED = ("saas-tenancy", "document-intake")
"""Examples the index names as planned, which have no directory yet."""
LEFT = ("migrations", "__pycache__", ".*_cache", ".env", ".venv", "*.sqlite3*")
"""What a reader's own runs leave in an example: never copied nor read."""
LEFT_BEHIND = shutil.ignore_patterns(*LEFT)
_PYTHON = re.compile(r"^```python\n(.*?)^```$", re.DOTALL | re.MULTILINE)


class Example(Project):
    """A copy of an example, with its migrations made, as its README makes them."""

    def __init__(self, name: str, root: Path) -> None:
        """Copy ``examples/<name>`` to ``root``, and make its first migration."""
        shutil.copytree(EXAMPLES / name, root, ignore=LEFT_BEHIND)
        super().__init__(root)
        self.prefix, self.passed = PASSED[name]
        self.env = {
            key: value
            for key, value in self.env.items()
            if not key.startswith(self.prefix)
        }
        workflow = yaml.safe_load(
            (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        self.job = workflow["jobs"]["test"]
        made = self.run("manage.py", "makemigrations", "-m", name, env=self.job["env"])
        assert made.returncode == 0, made.stdout + made.stderr


@pytest.fixture(params=sorted(PASSED))
def example(request: pytest.FixtureRequest, tmp_path: Path) -> Example:
    """Each example, copied and migrated."""
    name: str = request.param
    return Example(name, tmp_path / name)


def test_every_example_is_tested_and_listed() -> None:
    """A new example fails until this module knows how many tests it passes.

    The index lists each, and the planned ones it names have no directory.
    """
    found = sorted(path.parent.name for path in EXAMPLES.glob("*/pyproject.toml"))
    index = (EXAMPLES / "README.md").read_text(encoding="utf-8")

    assert found == sorted(PASSED)
    assert [name for name in PASSED if f"]({name}/README.md)" not in index] == []
    assert [name for name in PLANNED if f"`{name}`" not in index] == []
    assert not [name for name in PLANNED if (EXAMPLES / name).exists()]


@pytest.mark.parametrize("name", sorted(PASSED))
def test_an_example_depends_on_this_release(name: str) -> None:
    """Its domainkit range holds this version: a reader's install runs it.

    It was made on 2.8, and keeps the range ``init`` wrote, up to the next
    major version, which may break it.
    """
    project = tomllib.loads(
        (EXAMPLES / name / "pyproject.toml").read_text(encoding="utf-8")
    )
    [dependency] = [
        Requirement(each.replace(" (", "").replace(")", ""))
        for each in project["project"]["dependencies"]
    ]
    own = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert dependency.name == "domainkit"
    assert dependency.specifier.contains(own["project"]["version"])
    assert set(dependency.extras) <= set(own["project"]["optional-dependencies"])
    assert project["tool"]["mypy"]["strict"] is True


@pytest.mark.skipif(
    find_spec("mypy") is None or find_spec("ruff") is None,
    reason="needs mypy and ruff",
)
@pytest.mark.timeout(func_only=True)
def test_an_example_passes_its_ci_workflow(example: Example) -> None:
    """Every step of its test job but install and audit: lint, types, check, tests.

    The steps that read the database run one after another, as CI runs
    them; ruff, mypy, and the tests (on a database of their own) run beside
    them.
    """
    programs = {
        "python": [],
        "ruff": ["-m", "ruff"],
        "mypy": ["-m", "mypy", "--no-incremental"],
        "pytest": ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
    }
    lines = [
        line
        for step in example.job["steps"]
        if "run" in step and step["name"] not in {"Install", "Audit"}
        for line in step["run"].splitlines()
    ]
    sequential = [line for line in lines if line.startswith("python ")]
    started = {
        line: example.start(*programs[program], *args, env=example.job["env"])
        for line in lines
        if line not in sequential
        for program, *args in [shlex.split(line)]
    }
    for line in sequential:
        program, *args = shlex.split(line)
        done = example.run(*programs[program], *args, env=example.job["env"])
        assert done.returncode == 0, f"{line}\n{done.stdout}{done.stderr}"
    ended = {line: example.ended(process) for line, process in started.items()}

    for line, done in ended.items():
        assert done.returncode == 0, f"{line}\n{done.stdout}{done.stderr}"
    assert lines == [
        "ruff check .",
        "ruff format --check .",
        "mypy",
        "python manage.py check",
        "python manage.py migrate",
        'python manage.py makemigrations --check -m "unused"',
        "pytest",
    ]
    assert f"{example.passed} passed" in ended["pytest"].stdout


@pytest.mark.skipif(
    not os.environ.get("DOMAINKIT_TEST_POSTGRES_URL"),
    reason="set DOMAINKIT_TEST_POSTGRES_URL to a PostgreSQL server",
)
@pytest.mark.timeout(func_only=True)
def test_an_examples_tests_pass_on_postgresql(example: Example) -> None:
    """The same tests, on a test database of their own on the server."""
    server = make_url(os.environ["DOMAINKIT_TEST_POSTGRES_URL"])
    database = f"example_{uuid4().hex[:12]}"
    on_postgres = {
        f"{example.prefix}TEST_DATABASE_URL": server.set(
            database=database
        ).render_as_string(hide_password=False)
    }
    try:
        tested = example.run(
            "-m", "pytest", "-q", "-p", "no:cacheprovider", env=on_postgres
        )
    finally:
        on_server(
            server.render_as_string(hide_password=False),
            f'DROP DATABASE IF EXISTS "test_{database}" WITH (FORCE)',
        )

    assert tested.returncode == 0, f"{tested.stdout}{tested.stderr}"
    assert f"{example.passed} passed" in tested.stdout


def unindented(text: str) -> str:
    """``text``, each line without its indentation.

    So a block cut from a function's body matches the file it is in.
    """
    return "\n".join(line.strip() for line in text.splitlines())


@pytest.mark.parametrize("name", sorted(PASSED))
def test_an_examples_walkthrough_quotes_its_code(name: str) -> None:
    """Each Python block of its README is code the example holds, as it is there.

    So the walkthrough cannot drift from the code its tests run.
    """
    readme = (EXAMPLES / name / "README.md").read_text(encoding="utf-8")
    code = [
        f"\n{unindented(path.read_text(encoding='utf-8'))}\n"
        for path in (EXAMPLES / name).rglob("*.py")
        if not any(
            fnmatch(part, pattern)
            for part in path.relative_to(EXAMPLES / name).parts
            for pattern in LEFT
        )
    ]
    blocks = [textwrap.dedent(block) for block in _PYTHON.findall(readme)]

    assert blocks, "a walkthrough shows its code"
    assert [  # Whole lines: a block may not start or end inside one.
        block.splitlines()[0]
        for block in blocks
        if not any(f"\n{unindented(block)}\n" in text for text in code)
    ] == []
    for block in blocks:  # Whole statements, not a cut through one.
        compile(block, name, "exec", ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    assert re.findall(r"^## (.+)$", readme, re.MULTILINE)[0] == "The problem"


def test_the_examples_stay_out_of_the_distributions() -> None:
    """The wheel and the sdist hold the package, its README and its LICENSE.

    poetry-core builds both from ``packages`` alone, with no ``include``,
    so ``examples/`` (and their projects' own pyproject.toml files) never
    ship.
    """
    poetry = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["poetry"]

    assert poetry == {"packages": [{"include": "domainkit", "from": "src"}]}
