"""The lines a change adds under ``src/`` that no test ran, for CI's coverage job.

CI's ``coverage`` job runs this on a pull request's merge commit, once
every run is combined and written as JSON (``coverage json``). It reads
the lines each Python file under ``src/`` adds or changes since ``BASE``
(``git diff -U0 -M --merge-base``) and, from the report, the lines that
never ran and the branches never taken. An added line that never ran, or
from which a branch was never taken, is listed, and the script exits 1
(ADR 0191).

A line coverage leaves out (``exclude_also``, ``# pragma: no cover``) is
not measured, so it is never listed. A file the report does not hold is
an empty one (``skip_empty``), unless it holds code: then nothing
measured it, which is listed too, rather than passed unseen.

Usage: ``python .github/scripts/changed_coverage.py BASE [REPORT]``, from
the repository's root: ``BASE`` is the commit to compare with
(``HEAD^1`` on a pull request's merge commit, ``origin/stg`` for
``make cov``), ``REPORT`` the JSON report (``coverage.json``).
"""

import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_FILE = re.compile(r"^\+\+\+ b/(?P<path>.+)$", re.MULTILINE)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def added_lines(diff: str) -> dict[str, set[int]]:
    """The lines of each new file a unified diff (``-U0``) adds or changes.

    A hunk that only deletes (``+N,0``) adds nothing.
    """
    added: dict[str, set[int]] = {}
    lines: set[int] = set()
    for line in diff.splitlines():
        if named := _FILE.match(line):
            lines = added.setdefault(named["path"], set())
        elif hunk := _HUNK.match(line):
            start, count = int(hunk["start"]), int(hunk["count"] or 1)
            lines.update(range(start, start + count))
    return {path: found for path, found in added.items() if found}


def missed(report: Mapping[str, Any]) -> dict[str, set[int]]:
    """Per file of the report: the lines that never ran, or left a branch untaken."""
    root = Path.cwd().resolve()
    found = {}
    for name, measured in report["files"].items():
        path = Path(name)
        if path.is_absolute():
            path = path.resolve().relative_to(root)
        found[path.as_posix()] = set(measured["missing_lines"]) | {
            start for start, _ in measured.get("missing_branches", ())
        }
    return found


def holds_code(path: Path) -> bool:
    """Whether the module at ``path`` holds anything but a docstring."""
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]
    return bool(body)


def unrun(added: Mapping[str, set[int]], report: Mapping[str, Any]) -> dict[str, Any]:
    """The added lines under ``src/`` no test ran, per file; ``None``: none measured."""
    missing = missed(report)
    found: dict[str, Any] = {}
    for path, lines in sorted(added.items()):
        if not (path.startswith("src/") and path.endswith(".py")):
            continue
        if path not in missing:
            if holds_code(Path(path)):
                found[path] = None
            continue
        if left := sorted(lines & missing[path]):
            found[path] = left
    return found


def spans(lines: Iterable[int]) -> str:
    """``1, 3-5``: the lines, runs of them joined."""
    runs: list[list[int]] = []
    for line in sorted(lines):
        if runs and line == runs[-1][1] + 1:
            runs[-1][1] = line
        else:
            runs.append([line, line])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def main(argv: list[str]) -> int:
    """List the added lines no test ran since ``argv[0]``; 1 if there are any."""
    if len(argv) not in (1, 2):
        print(__doc__, file=sys.stderr)  # noqa: T201 - a command-line script.
        return 2
    base, report = argv[0], Path(argv[1] if len(argv) == 2 else "coverage.json")
    command = ["git", "diff", "-U0", "-M", "--diff-filter=AMR", "--merge-base"]
    diff = subprocess.run(  # noqa: S603 - our own arguments, no shell.
        [*command, base, "--", "src/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = unrun(added_lines(diff), json.loads(report.read_text(encoding="utf-8")))
    for path, lines in found.items():
        what = "not measured" if lines is None else f"lines {spans(lines)}"
        print(f"{path}: {what}")  # noqa: T201 - the job's log.
    if found:
        print(  # noqa: T201
            "Each line a change adds under src/ runs in a test, and takes each "
            "of its branches (ADR 0191).",
            file=sys.stderr,
        )
        return 1
    print("Every line added under src/ ran, each branch taken.")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
