"""The mutant name patterns of the functions a change touches.

CI's ``mutation (changed)`` job runs this on a pull request's merge commit,
then ``mutmut run`` with what it prints: only the mutants of the functions
and methods under ``src/`` whose lines the pull request added or changed.
mutmut names a mutant after the function it changes: ``module.x_name`` for
a function, ``module.xǁClassǁname`` for a method of a top-level class,
then ``__mutmut_<n>``. Code outside them (imports, constants, class
bodies) has no mutants of its own and is left out; so is a function only
deleted.

Prints the patterns on one line, separated by spaces; nothing when the
change touches no function. Writes ``changed-mutants.txt`` with one pattern
per line, for the job summary.

Usage: ``python .github/scripts/changed_mutants.py BASE``, from the
repository's root, with ``BASE`` the commit to compare with (``HEAD^1``,
the base branch, on a pull request's merge commit).
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

SOURCE = Path("src")
SEPARATOR = "ǁ"
"""mutmut's separator of a method's class and name."""
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def changed_lines(diff: str) -> set[int]:
    """The lines of the new file that a unified diff (``-U0``) adds or changes."""
    lines: set[int] = set()
    for match in _HUNK.finditer(diff):
        start, count = int(match[1]), int(match[2] or 1)
        lines.update(range(start, start + count))
    return lines


def module_of(path: Path) -> str:
    """``src/domainkit/db/fixtures.py``: ``domainkit.db.fixtures``."""
    parts = path.relative_to(SOURCE).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def touched(source: str, lines: set[int]) -> list[str]:
    """The names mutmut gives the functions and methods ``lines`` fall in.

    A function's lines run from its first decorator to its last line.
    """
    names: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if _spans(node, lines):
                names.append(f"x_{node.name}")
        elif isinstance(node, ast.ClassDef):
            names += [
                f"x{SEPARATOR}{node.name}{SEPARATOR}{method.name}"
                for method in node.body
                if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
                and _spans(method, lines)
            ]
    return names


def _spans(node: ast.FunctionDef | ast.AsyncFunctionDef, lines: set[int]) -> bool:
    """Whether any of ``lines`` is one of ``node``'s."""
    first = min([node.lineno, *(each.lineno for each in node.decorator_list)])
    return any(first <= line <= (node.end_lineno or node.lineno) for line in lines)


def patterns(base: str) -> list[str]:
    """The mutant name patterns of what changed since ``base``."""
    found: list[str] = []
    for name in sorted(
        _git("diff", "--name-only", "--diff-filter=AM", base, "--", "src").split()
    ):
        path = Path(name)
        if path.suffix != ".py":
            continue
        lines = changed_lines(_git("diff", "-U0", base, "--", name))
        source = path.read_text(encoding="utf-8")
        found += [
            f"{module_of(path)}.{function}__mutmut_*"
            for function in touched(source, lines)
        ]
    return found


def _git(*args: str) -> str:
    """What ``git args`` printed."""
    return subprocess.run(  # noqa: S603 - our own arguments, no shell.
        ["git", *args],  # noqa: S607 - git on PATH, as in CI.
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main(argv: list[str]) -> int:
    """Print the patterns of what changed since ``argv[0]``."""
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)  # noqa: T201 - a command-line script.
        return 2
    found = patterns(argv[0])
    Path("changed-mutants.txt").write_text("".join(f"{each}\n" for each in found))
    print(" ".join(found))  # noqa: T201 - read by the job.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
