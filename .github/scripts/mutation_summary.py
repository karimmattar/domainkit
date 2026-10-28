"""One mutation shard's result: counts, a score, and the mutants that lived.

CI's mutation job runs this after ``mutmut run`` with the shard's mutant name
patterns. mutmut keeps a result for every mutant of the package in
``mutants/**/*.meta``; a shard checks only its own, so the others read "not
checked" and are left out here. Prints Markdown for the job summary, writes
the same numbers to ``mutation-summary.json`` and the mutants no test caught
to ``mutmut-results.txt``, and exits 1 when a mutant of the shard was never
checked, or its check was interrupted: the run stopped before the end, so
its score is no baseline.

With ``--lived-fails`` after the shard's name, as CI's ``mutation
(changed)`` job gives it, it also exits 1 when a mutant lived: the shard is
a pull request's changed functions, whose authors kill each one or say why
it stays.

Usage: ``python .github/scripts/mutation_summary.py SHARD [--lived-fails]
PATTERN...``, from the directory that holds ``mutants/``.
"""

import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from mutmut.stats import status_by_exit_code

# Killed and timed-out mutants were caught by a test, and the type checker's
# catch counts too; skipped and unchecked ones are left out of the score; the
# rest lived.
CAUGHT = frozenset({"killed", "timeout", "caught by type check"})
UNCHECKED = frozenset({"not checked", "check was interrupted by user"})
LEFT_OUT = UNCHECKED | {"skipped"}


@dataclass(frozen=True)
class Summary:
    """A shard's counts by status, and the share of scored mutants caught."""

    shard: str
    mutants: int
    counts: dict[str, int]
    score: float | None
    complete: bool


def results(mutants: Path, patterns: list[str]) -> dict[str, str]:
    """The status of every mutant whose name matches one of ``patterns``."""
    found: dict[str, str] = {}
    for meta in sorted(mutants.rglob("*.meta")):
        exit_codes = json.loads(meta.read_text())["exit_code_by_key"]
        for name, exit_code in exit_codes.items():
            if any(fnmatchcase(name, pattern) for pattern in patterns):
                found[name] = status_by_exit_code[exit_code]
    return found


def summarise(shard: str, statuses: dict[str, str]) -> Summary:
    """Count ``statuses`` and score the share of them that a test caught."""
    counts = Counter(statuses.values())
    scored = sum(n for status, n in counts.items() if status not in LEFT_OUT)
    caught = sum(counts[status] for status in CAUGHT)
    return Summary(
        shard=shard,
        mutants=len(statuses),
        counts=dict(sorted(counts.items())),
        score=round(100 * caught / scored, 2) if scored else None,
        complete=bool(statuses) and not any(counts[each] for each in UNCHECKED),
    )


def markdown(summary: Summary, lived: list[str]) -> str:
    """The job summary: a table of counts, then the mutants that lived."""
    score = "n/a" if summary.score is None else f"{summary.score}%"
    lines = [
        f"### Mutation shard `{summary.shard}`",
        "",
        (
            f"{summary.mutants} mutants; score {score} (caught, of those"
            " neither skipped nor unchecked)."
        ),
        "",
        "| Status | Mutants |",
        "| --- | ---: |",
        *(f"| {status} | {n} |" for status, n in summary.counts.items()),
        "",
    ]
    if not summary.complete:
        unchecked = sum(summary.counts.get(each, 0) for each in UNCHECKED)
        lines += [
            (
                f"**Incomplete:** {unchecked} of {summary.mutants} mutants were"
                " never checked, or were interrupted, so this is not a baseline."
            ),
            "",
        ]
    if lived:
        lines += [
            f"<details><summary>{len(lived)} mutants no test caught</summary>",
            "",
            "```",
            *lived,
            "```",
            "</details>",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Summarise shard ``argv[0]`` over the mutant name patterns after it."""
    shard, *patterns = argv or [""]
    lived_fails = patterns[:1] == ["--lived-fails"]
    patterns = patterns[lived_fails:]
    if not shard or not patterns:  # A shard name and at least one pattern.
        print(__doc__, file=sys.stderr)  # noqa: T201 - a command-line script.
        return 2
    statuses = results(Path("mutants"), patterns)
    summary = summarise(shard, statuses)
    lived = sorted(
        f"{name}: {status}"
        for name, status in statuses.items()
        if status not in CAUGHT | LEFT_OUT
    )
    Path("mutation-summary.json").write_text(
        json.dumps(asdict(summary), indent=2) + "\n"
    )
    Path("mutmut-results.txt").write_text("".join(f"{line}\n" for line in lived))
    print(markdown(summary, lived))  # noqa: T201 - the job summary.
    return 0 if summary.complete and not (lived_fails and lived) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
