"""The benchmark ladder: what a request costs on each rung, gated in CI.

Not shipped in the wheel. ``python -m benchmarks`` runs it; see
docs/performance.md for the rungs, the budgets, and how to update them.
``counting`` holds the exact counters, ``ladder`` the rungs, ``rung`` the
code that measures one rung in a process of its own, and ``budgets`` the
check against ``budgets.toml``.
"""

import sys

STARTUP_MODULES: frozenset[str] = frozenset(sys.modules)
"""What the interpreter held before the harness imported anything (``site``,
the codecs): a rung's ``modules`` counts what it imported past these."""
