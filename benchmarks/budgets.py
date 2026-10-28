"""Budgets: what a request may cost on each rung, and the check against them.

``benchmarks/budgets.toml`` holds one number per rung and measure, and a
tolerance per measure. A run over budget fails, naming the rung, the
measure, the budget, and what was measured. A run under budget by more than
the tolerance prints a hint to lower it, so a gain stays won::

    [tolerance]
    calls = 0.02       # a fraction of the budget
    statements = 0.0   # per request, absolute

    [rungs.me]
    calls = 5295
    statements = 5
"""

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

MEASURES: Final = (
    "calls",
    "statements",
    "transactions",
    "checkouts",
    "pings",
    "modules",
)
"""What is gated: exact counts per request, and modules per process; never time."""
RELATIVE: Final = frozenset({"calls"})
"""Measures whose tolerance is a fraction of the budget; the others' is absolute."""
DEFAULT_TOLERANCE: Final = {
    "calls": 0.02,
    "statements": 0.0,
    "transactions": 0.0,
    "checkouts": 0.0,
    "pings": 0.0,
    "modules": 0.0,
}
"""Calls may grow by 2%; round trips and modules are exact."""
EPSILON: Final = 1e-9

type Kind = Literal["over", "under", "unbudgeted", "unmeasured"]


@dataclass(frozen=True, slots=True)
class Budgets:
    """The budgets of each rung, and how far a measure may stray."""

    rungs: Mapping[str, Mapping[str, float]]
    tolerance: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_TOLERANCE)
    )

    def allowance(self, measure: str, budget: float) -> float:
        """How far ``measure`` may go past ``budget``, either way, before it counts."""
        tolerance = self.tolerance.get(measure, 0.0)
        return budget * tolerance if measure in RELATIVE else tolerance


@dataclass(frozen=True, slots=True)
class Verdict:
    """One finding of the check."""

    kind: Kind
    rung: str
    measure: str = ""
    budget: float = 0.0
    actual: float = 0.0

    @property
    def fails(self) -> bool:
        """Whether it fails the run (strictly, a missing budget does too)."""
        return self.kind == "over"

    def message(self) -> str:
        """One line for people."""
        if self.kind == "unbudgeted":
            return f"{self.rung}: no budget for {self.measure}; add one."
        if self.kind == "unmeasured":
            return f"{self.rung}: has budgets but was not measured."
        where = f"{self.rung} {self.measure}"
        figures = f"budget {_number(self.budget)}, measured {_number(self.actual)}"
        if self.kind == "over":
            return f"OVER BUDGET: {where}: {figures}."
        return f"Under budget: {where}: {figures}; lower the budget to lock it in."


def load(path: Path) -> Budgets:
    """Read a budgets file.

    Raises:
        ValueError: A rung's budget names an unknown measure, or is negative.
    """
    data = tomllib.loads(path.read_text())
    tolerance = {
        name: float(value) for name, value in data.get("tolerance", {}).items()
    }
    rungs: dict[str, dict[str, float]] = {}
    for rung, measures in data.get("rungs", {}).items():
        rungs[rung] = {}
        for measure, value in measures.items():
            if measure not in MEASURES or float(value) < 0:
                raise ValueError(f"{path}: rungs.{rung}.{measure} = {value!r}")
            rungs[rung][measure] = float(value)
    for measure, value in tolerance.items():
        if measure not in MEASURES or value < 0:
            raise ValueError(f"{path}: tolerance.{measure} = {value!r}")
    return Budgets(rungs, tolerance)


def check(
    budgets: Budgets,
    results: Mapping[str, Mapping[str, float]],
    *,
    expected: Iterable[str] = (),
) -> list[Verdict]:
    """Compare what each rung measured with its budgets.

    ``expected`` names the rungs that should have run: a budgeted one that
    did not is reported, as is a measure without a budget.
    """
    verdicts = [
        Verdict("unmeasured", rung)
        for rung in expected
        if rung in budgets.rungs and rung not in results
    ]
    for rung, measured in results.items():
        planned = budgets.rungs.get(rung, {})
        for measure in MEASURES:
            if measure not in planned:
                verdicts.append(Verdict("unbudgeted", rung, measure))
                continue
            budget, actual = planned[measure], measured[measure]
            allowance = budgets.allowance(measure, budget) + EPSILON
            if actual > budget + allowance:
                verdicts.append(Verdict("over", rung, measure, budget, actual))
            elif actual < budget - allowance:
                verdicts.append(Verdict("under", rung, measure, budget, actual))
    return verdicts


def render(budgets: Budgets, results: Mapping[str, Mapping[str, float]]) -> str:
    """A budgets file holding what was measured, with the same tolerances.

    Calls are rounded up to a whole call, round trips to a hundredth.
    """
    lines = [
        "# What a request may cost on each rung of the ladder (docs/performance.md).",
        "# Over budget fails CI; well under prints a hint to lower the budget.",
        "# Rewrite it from a run with: python -m benchmarks --write-budgets",
        "",
        "[tolerance]",
    ]
    lines += [
        f"{measure} = {_number(budgets.tolerance[measure])}"
        for measure in MEASURES
        if measure in budgets.tolerance
    ]
    for rung, measured in results.items():
        lines += ["", f"[rungs.{rung}]"]
        for measure in MEASURES:
            value = measured[measure]
            rounded = -(-value // 1) if measure in RELATIVE else round(value, 2)
            lines.append(f"{measure} = {_number(rounded)}")
    return "\n".join(lines) + "\n"


def _number(value: float) -> str:
    """``5`` for whole numbers, ``0.02`` otherwise."""
    return str(int(value)) if value == int(value) else f"{value:g}"
