"""C8, the proof: the same shop on the 1.0 layout and on the new core.

One aggregate, ``Product``, with four commands, made by the generators in
two new projects: as a layered domain (packages for the domain, the
application, the infrastructure, and the API, as 1.0 wrote them) and as a
four-file domain. The project's own tests run, so both work; then each is
measured, and the figures must be the ones docs/roadmap.md records.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

from domainkit.management import generators as generators_module
from domainkit.management.commands import startdomain
from domainkit_tests.test_generators import Project, run

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = [
    ("PublishProduct", "at:datetime"),
    ("RepriceProduct", "price:decimal:10:2"),
    ("RestockProduct", "quantity:int"),
    ("DiscontinueProduct", None),
]
"""The shop's four commands, the same in both layouts."""
LAYOUTS = {"layered": "shop", "four-file": "shop"}
"""Each layout's domain, in a project of its own."""


def code_lines(source: str) -> int:
    """Lines holding code: no blank lines, comments, or docstrings."""
    docstrings: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            first = body[0]
            docstrings.update(
                range(first.lineno, (first.end_lineno or first.lineno) + 1)
            )
    lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in {
            tokenize.COMMENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENDMARKER,
        }:
            continue
        lines.update(range(token.start[0], token.end[0] + 1))
    return len(lines - docstrings)


def measure(directory: Path) -> tuple[int, int]:
    """``directory``'s Python files that hold code, and their lines of code."""
    counted = [code_lines(path.read_text()) for path in sorted(directory.rglob("*.py"))]
    return sum(1 for each in counted if each), sum(counted)


def recorded() -> dict[str, tuple[int, int, int]]:
    """The figures docs/roadmap.md records, by layout."""
    text = (ROOT / "docs" / "roadmap.md").read_text()
    rows = re.findall(
        r"^\| (layered|four-file) \| (\d+) \| (\d+) \| (\d+) \|", text, re.M
    )
    return {layout: (int(a), int(b), int(c)) for layout, a, b, c in rows}


def build(
    root: Path, layout: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int, int]:
    """The shop in ``layout``, in a new project at ``root``, measured.

    Its files and lines of code, and the lines each command adds.
    """
    domain = LAYOUTS[layout]
    project = Project(root)
    run("init", "proof", str(root))
    with monkeypatch.context() as patched:
        patched.setattr(generators_module, "project_root", lambda: root)
        patched.setattr(startdomain, "project_root", lambda: root)
        patched.syspath_prepend(str(root / "src"))
        extra = ["--layered"] if layout == "layered" else []
        run("startdomain", domain, "--aggregate", "Product", *extra)
        [package] = {path.parent for path in root.rglob(f"src/**/{domain}/__init__.py")}
        _, before = measure(package)
        for command, field in COMMANDS:
            fields = ["--field", field] if field else []
            run("addcommand", domain, "Product", command, *fields)
    for step in (("makemigrations", "-m", "shop"), ("migrate",), ("check",)):
        project.finished(project.start("manage.py", *step))
    tested = project.finished(
        project.start("-m", "pytest", "-q", "-p", "no:cacheprovider")
    )
    assert " passed" in tested
    assert "failed" not in tested
    files, lines = measure(package)
    return files, lines, (lines - before) // len(COMMANDS)


@pytest.mark.timeout(func_only=True)
def test_the_shop_is_smaller_on_the_new_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both shops pass their own tests; their sizes are what roadmap.md says."""
    measured = {
        layout: build(tmp_path / layout, layout, monkeypatch) for layout in LAYOUTS
    }

    assert measured == recorded(), measured
    assert measured["four-file"][1] < measured["layered"][1]
