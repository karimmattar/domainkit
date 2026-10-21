"""Climb the ladder: measure each rung in a process of its own, then gate.

::

    python -m benchmarks                          # the rungs without a database
    export DOMAINKIT_BENCH_POSTGRES_URL=postgresql+asyncpg://...
    python -m benchmarks                          # all of them
    python -m benchmarks me read                  # some
    python -m benchmarks --baseline ../main/src   # side by side with another tree

Each source tree (``--src``, and ``--baseline`` when given) is measured with
this harness, its rungs interleaved with the other tree's so drift on a
shared machine hits both. Database rungs get a PostgreSQL schema per tree,
migrated by that tree's code and dropped at the end. Counts are checked
against ``budgets.toml`` (``--src`` only); CPU time is reported, never
gated. ``GITHUB_STEP_SUMMARY`` (or ``--summary``) gets a Markdown table.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from benchmarks import budgets as budgeting
from benchmarks.ladder import LADDER, POSTGRES_VARIABLE, RUNGS, Rung

ROOT: Final = Path(__file__).resolve().parent.parent
"""The repository holding this harness."""
BUDGETS: Final = Path(__file__).resolve().parent / "budgets.toml"
IN_PROCESS: Final = {"warmup": 300, "count": 200, "requests": 2000, "repeats": 5}
WITH_DATABASE: Final = {"warmup": 100, "count": 100, "requests": 300, "repeats": 3}


@dataclass
class Tree:
    """A domainkit source tree to measure, and the interpreter that runs it."""

    label: str
    src: Path
    python: str
    schema: str = ""
    fixture: Path | None = None
    unprepared: str = ""
    """Why its database could not be made, if it could not."""
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def environment(self, migrations: Path, postgres: str) -> dict[str, str]:
        """Variables for a child: this tree first on the path, then the harness.

        The database's URL goes here, not on a command line others can read.
        """
        return {
            **os.environ,
            POSTGRES_VARIABLE: postgres,
            "PYTHONPATH": os.pathsep.join([str(self.src), str(ROOT)]),
            "PYTHONHASHSEED": "0",
            "DOMAINKIT_PROJECT_ROOT": str(ROOT / "benchmarks" / "project"),
            "DOMAINKIT_ENV_PREFIX": "BENCH_",
            "DOMAINKIT_BENCH_MIGRATIONS": str(migrations),
        }


def parse(argv: Sequence[str]) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description=__doc__.split("::")[0] if __doc__ else "",
    )
    parser.add_argument("rungs", nargs="*", help="rungs to run (default: all)")
    parser.add_argument("--src", type=Path, default=ROOT / "src")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--baseline", type=Path, help="another tree's src, to compare")
    parser.add_argument("--baseline-python", help="its interpreter (default --python)")
    parser.add_argument(
        "--postgres",
        default=os.environ.get(POSTGRES_VARIABLE, ""),
        help=f"a database to make schemas in (default ${POSTGRES_VARIABLE})",
    )
    parser.add_argument("--budgets", type=Path, default=BUDGETS)
    parser.add_argument("--no-gate", action="store_true", help="report only")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on rungs or measures without budgets",
    )
    parser.add_argument(
        "--write-budgets", action="store_true", help="rewrite budgets from this run"
    )
    parser.add_argument("--json", type=Path, help="write every figure here")
    parser.add_argument("--summary", type=Path, help="Markdown table (CI's summary)")
    parser.add_argument("--profile-dir", type=Path, help="keep each rung's cProfile")
    for name in IN_PROCESS:
        parser.add_argument(f"--{name}", type=int, help="override for every rung")
    options = parser.parse_args(argv)
    unknown = sorted(set(options.rungs) - set(RUNGS))
    if unknown:
        parser.error(f"unknown rungs: {', '.join(unknown)}; known: {', '.join(RUNGS)}")
    return options


def chosen(options: argparse.Namespace) -> list[Rung]:
    """The rungs to run, in ladder order; database rungs only with PostgreSQL."""
    names = set(options.rungs) or set(RUNGS)
    return [
        rung
        for rung in LADDER
        if rung.name in names and (options.postgres or not rung.database)
    ]


def child(
    tree: Tree, arguments: list[str], migrations: Path, postgres: str
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m <arguments>`` for ``tree``."""
    return subprocess.run(  # noqa: S603 - our own interpreter and modules.
        [tree.python, "-m", *arguments],
        env=tree.environment(migrations, postgres),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def prepare(tree: Tree, postgres: str, scratch: Path) -> None:
    """Make ``tree``'s schema and fixture, migrated by its own code.

    Raises:
        RuntimeError: The schema could not be made.
    """
    schema = f"domainkit_bench_{tree.label}_{os.getpid()}"
    tree.fixture = scratch / f"{tree.label}.json"
    tree.schema = schema  # Dropped at the end, even half made.
    done = child(
        tree,
        ["benchmarks.database", "create", schema, str(tree.fixture)],
        scratch / f"migrations-{tree.label}",
        postgres,
    )
    if done.returncode:
        raise RuntimeError(f"{tree.label}: no database:\n{done.stderr[-3000:]}")


def measure(tree: Tree, rung: Rung, options: argparse.Namespace, scratch: Path) -> None:
    """Measure ``rung`` on ``tree`` in a fresh process; keep the result or failure."""
    sizes = WITH_DATABASE if rung.database else IN_PROCESS
    arguments = ["benchmarks.rung", rung.name]
    for name, default in sizes.items():
        value = getattr(options, name)
        arguments += [f"--{name}", str(default if value is None else value)]
    if rung.database:
        arguments += ["--schema", tree.schema, "--fixture", str(tree.fixture)]
    if options.profile_dir:
        options.profile_dir.mkdir(parents=True, exist_ok=True)
        profile = options.profile_dir / f"{tree.label}-{rung.name}.pstats"
        arguments += ["--profile-out", str(profile)]
    done = child(
        tree, arguments, scratch / f"migrations-{tree.label}", options.postgres
    )
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if done.returncode or not lines:
        tree.failures[rung.name] = done.stderr[-3000:] or done.stdout[-3000:]
    else:
        tree.results[rung.name] = json.loads(lines[-1])
    status = "failed" if rung.name in tree.failures else "done"
    sys.stderr.write(f"  {tree.label:8} {rung.name:13} {status}\n")


def table(trees: Sequence[Tree], rungs: Sequence[Rung]) -> list[list[str]]:
    """Rows of the report: one per rung, the trees side by side."""
    header = ["rung", "what"]
    for tree in trees:
        suffix = f" ({tree.label})" if len(trees) > 1 else ""
        header += [f"calls{suffix}", f"SQL{suffix}", f"CPU µs{suffix}"]
    rows = [header]
    for rung in rungs:
        row = [rung.name, rung.title]
        for tree in trees:
            found = tree.results.get(rung.name)
            if found is None:
                row += ["failed" if rung.name in tree.failures else "-", "", ""]
                continue
            cpu = found["cpu_us"]
            row += [
                f"{found['calls']:,.1f}",
                _round_trips(found),
                f"{cpu['best']:,.1f} ({_spread(cpu)})",
            ]
        rows.append(row)
    return rows


def _round_trips(found: Mapping[str, Any]) -> str:
    """``5 / 1 / 1 / 1``: statements, transactions, checkouts, pings."""
    keys = ("statements", "transactions", "checkouts", "pings")
    return " / ".join(f"{found[key]:g}" for key in keys)


def _spread(cpu: Mapping[str, float]) -> str:
    """How far the worst timed run was from the best, as a percentage."""
    return f"+{(cpu['worst'] - cpu['best']) / cpu['best'] * 100:.0f}%"


def text(rows: list[list[str]]) -> str:
    """The rows as an aligned plain-text table."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        for row in rows
    )


def markdown(rows: list[list[str]], notes: Sequence[str]) -> str:
    """The rows as a Markdown table, with the gate's findings below."""
    lines = ["## Benchmark ladder", ""]
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("|" + "---|" * len(rows[0]))
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    lines += [
        "",
        (
            "Calls and SQL (statements / transactions / checkouts / pings) are per "
            "request and gated against `benchmarks/budgets.toml`. CPU is the best "
            "timed run, with the worst run's distance from it: reported, not gated."
        ),
    ]
    if notes:
        lines += ["", *[f"- {note}" for note in notes]]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ladder; 1 if a rung failed or went over budget."""
    options = parse(sys.argv[1:] if argv is None else argv)
    trees = [Tree("current", options.src.resolve(), options.python)]
    if options.baseline:
        python = options.baseline_python or options.python
        trees.append(Tree("baseline", options.baseline.resolve(), python))
    rungs = chosen(options)
    skipped = [rung.name for rung in LADDER if rung.database and not options.postgres]
    with tempfile.TemporaryDirectory(prefix="domainkit-bench-") as directory:
        scratch = Path(directory)
        try:
            if options.postgres and any(rung.database for rung in rungs):
                for tree in trees:
                    try:
                        prepare(tree, options.postgres, scratch)
                    except RuntimeError as error:
                        if tree is trees[0]:
                            raise
                        tree.unprepared = str(error)  # Only the comparison is lost.
            for number, rung in enumerate(rungs):
                order = trees if number % 2 == 0 else trees[::-1]
                for tree in order:
                    if rung.database and tree.unprepared:
                        tree.failures[rung.name] = tree.unprepared
                        continue
                    measure(tree, rung, options, scratch)
        finally:
            for tree in trees:
                if tree.schema:
                    child(
                        tree,
                        ["benchmarks.database", "drop", tree.schema],
                        scratch,
                        options.postgres,
                    )
    return report(options, trees, rungs, skipped)


def report(
    options: argparse.Namespace,
    trees: list[Tree],
    rungs: list[Rung],
    skipped: list[str],
) -> int:
    """Print the table and the gate's findings; write the files asked for."""
    current = trees[0]
    rows = table(trees, rungs)
    sys.stdout.write(text(rows) + "\n")
    notes = [f"{name}: skipped, no PostgreSQL (--postgres)" for name in skipped]
    failed = bool(current.failures)
    for tree in trees:
        for name, error in tree.failures.items():
            sys.stderr.write(f"\n{tree.label} {name} failed:\n{error}\n")
            notes.append(f"{tree.label} {name}: failed (see the log)")
    verdicts: list[budgeting.Verdict] = []
    exists = options.budgets.exists()
    planned = budgeting.load(options.budgets) if exists else budgeting.Budgets({})
    if options.write_budgets:
        options.budgets.write_text(budgeting.render(planned, current.results))
        notes.append(f"Budgets rewritten from this run: {options.budgets}")
    elif not options.no_gate:
        # Strictly, every rung is expected: one skipped is not one gated.
        expected = LADDER if options.strict and not options.rungs else rungs
        verdicts = budgeting.check(
            planned, current.results, expected=[rung.name for rung in expected]
        )
    for verdict in verdicts:
        notes.append(verdict.message())
        failed |= verdict.fails or (options.strict and verdict.kind != "under")
    for note in notes:
        sys.stdout.write(note + "\n")
    summary = options.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as output:
            output.write(markdown(rows, notes))
    if options.json:
        figures = {tree.label: tree.results for tree in trees}
        options.json.write_text(json.dumps(figures, indent=2) + "\n")
    return 1 if failed else 0
