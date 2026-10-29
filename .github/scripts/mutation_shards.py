"""Hand the mutants ``mutation (changed)`` left unchecked to shards.

CI's ``mutation (changed)`` job runs the changed functions' mutants itself,
and stops mutmut before its time is up. A pull request of a typical size
finishes there. A larger one leaves mutants unchecked: ``plan`` splits
them into shards, balanced by the time their tests take (mutmut's own
durations, in ``mutants/mutmut-stats.json``), and writes them to
``mutation-shards.json``; each shard job takes that file and the stats,
which spare it mutmut's run of the whole suite, and runs the mutants
``names`` gives it.

``plan`` prints ``shards=`` followed by the shards' numbers as a JSON list
(``shards=[1,2,3]``), for ``$GITHUB_OUTPUT`` and the shard job's matrix;
nothing after ``=`` when no mutant is left. It makes as many shards as
their mutants need, at about ``SHARD`` seconds of a worker's time each, and
``SHARDS`` at most.

Usage, from the directory that holds ``mutants/``:
``python .github/scripts/mutation_shards.py plan PATTERN...``, then
``python .github/scripts/mutation_shards.py names N``.
"""

import heapq
import json
import math
import sys
from pathlib import Path
from typing import Any

from mutation_summary import UNCHECKED, results

PLAN = Path("mutation-shards.json")
STATS = Path("mutants") / "mutmut-stats.json"
START = 4.0
"""A worker's seconds a mutant takes besides its tests (ADR 0106)."""
SHARE = 0.35
"""The share of its tests' time a mutant takes on average: most fail one
early, and mutmut stops at the first failure (ADR 0106)."""
SHARD = 5000.0
"""A worker's seconds of mutants in one shard: 21 minutes on its four,
besides its clean run of their tests, which can take 15."""
SHARDS = 12
"""The most shards one pull request starts."""
UNIT = 100
"""The most mutants of one function a shard takes together, and at most
half a shard's time."""


def units(names: list[str], stats: dict[str, Any]) -> dict[tuple[str, ...], float]:
    """``names`` grouped by function, ``UNIT`` or half a shard at most, with cost.

    A group costs a worker's seconds: ``START`` a mutant, and ``SHARE`` of
    the time of the tests that reach its function.
    """
    tests = stats["tests_by_mangled_function_name"]
    duration = stats["duration_by_test"]
    by_function: dict[str, list[str]] = {}
    for name in names:
        by_function.setdefault(_function(name), []).append(name)
    found: dict[tuple[str, ...], float] = {}
    for function, mutants in by_function.items():
        reach = sum(duration.get(test, 0.0) for test in tests.get(function, []))
        each = START + SHARE * reach
        size = max(1, min(UNIT, int(SHARD / 2 / each)))
        for first in range(0, len(mutants), size):
            group = tuple(mutants[first : first + size])
            found[group] = each * len(group)
    return found


def _function(mutant: str) -> str:
    """``domainkit.a.x_f`` of ``domainkit.a.x_f__mutmut_3``, as mutmut names it."""
    return mutant.partition("__mutmut_")[0]


def split[T](cost: dict[T, float], count: int) -> list[list[T]]:
    """``cost``'s keys in ``count`` shards of about equal cost.

    The costliest first, each to the shard with the least so far.
    """
    shards: list[list[T]] = [[] for _ in range(count)]
    loads = [(0.0, index) for index in range(count)]
    for key in sorted(cost, key=lambda each: -cost[each]):
        load, index = heapq.heappop(loads)
        shards[index].append(key)
        heapq.heappush(loads, (load + cost[key], index))
    return shards


def plan(patterns: list[str]) -> list[list[str]]:
    """The shards of the mutants of ``patterns`` left unchecked, by name.

    A function's mutants stay together, ``UNIT`` at most: mutmut runs the
    tests of a shard's functions once, clean, before its mutants, so a
    function split across shards costs that run in each.
    """
    left = sorted(
        name
        for name, status in results(Path("mutants"), patterns).items()
        if status in UNCHECKED
    )
    if not left:
        return []
    cost = units(left, json.loads(STATS.read_text()))
    count = min(SHARDS, math.ceil(sum(cost.values()) / SHARD))
    return [
        sorted(name for group in shard for name in group)
        for shard in split(cost, count)
    ]


def main(argv: list[str]) -> int:
    """``plan PATTERN...`` or ``names N``."""
    match argv:
        case ["plan", *patterns] if patterns:
            shards = plan(patterns)
            PLAN.write_text(json.dumps({"shards": shards}, indent=2) + "\n")
            numbers = json.dumps(list(range(1, len(shards) + 1))) if shards else ""
            print(f"shards={numbers}")  # noqa: T201 - for $GITHUB_OUTPUT.
            return 0
        case ["names", number] if number.isdigit() and int(number) > 0:
            shards = json.loads(PLAN.read_text())["shards"]
            print(" ".join(shards[int(number) - 1]))  # noqa: T201 - read by the job.
            return 0
    print(__doc__, file=sys.stderr)  # noqa: T201 - a command-line script.
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
