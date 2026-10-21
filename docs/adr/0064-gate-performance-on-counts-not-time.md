# 0064. Gate performance in CI on counts, and report time

- Status: Accepted
- Date: 2026-10-21

## Context

Nothing stopped domainkit getting slower: CI ran no benchmark (re-audit
PERF-09, ASYNC-R17). A gate has to fail a pull request that makes a request
dearer, and pass every other one, on GitHub's shared runners.

Time is the measure people care about, and the one a shared runner cannot
give. On the 4-core machine the re-audit used, the same code timed in one
process spread by 10–15% from run to run, and whole series came in low and
did not reproduce; the ladder below spread by up to 65% on its cheapest
rung while other work ran. A gate on time needs a band wider than that, so
it lets a 10% regression through, and still fails now and then on nothing.

The options:

- **Time with a wide band** (pytest-benchmark, a script): misses what it
  should catch, flakes on what it should not.
- **Instruction counts under a simulator** (CodSpeed, cachegrind): exact,
  but a hosted service with a token, or a run many times slower, for one
  number that does not say why it moved.
- **What the code does, counted exactly**: Python function calls per
  request (cProfile counts them), and the database round trips SQLAlchemy
  sees: statements, transactions, pool checkouts, and pre-pings. They do
  not depend on the runner's speed, and when they move, a cProfile diff
  names the functions. They miss work that costs more without more calls
  (a slower regular expression, a bigger payload); time still shows that,
  to people.

## Decision

- A **benchmark ladder** in `benchmarks/`, not shipped: thirteen rungs
  from a bare ASGI application to a keyed write and a 429, each measured
  in a process of its own, with requests fed straight into the ASGI
  application. `python -m benchmarks` runs it; `--baseline` measures
  another source tree with the same harness, the two interleaved.
- **Gated: counts per request**, against one number per rung and measure
  in `benchmarks/budgets.toml`. Calls may exceed their budget by 2%; round
  trips not at all. Over budget fails the job, naming the rung, the
  measure, the budget and the value; more than the tolerance under budget
  prints a hint to lower the budget, so gains stay won.
- **Reported: CPU time per request**, the best of several timed runs with
  the worst run's distance from it, in the job summary, beside the base
  branch's on pull requests. Never gated.
- **Counting is exact by construction**: one request at a time on uvloop,
  `PYTHONHASHSEED=0`, the garbage collector held off inside the counted
  window, the logging thread flushed at both edges of it, and the window's
  own cost measured empty and taken off. cProfile counts every thread, so
  the JSON access log's formatting, done on the logging thread, counts.
- A `bench` job in CI on Python 3.13 with PostgreSQL as a service, on
  every push and pull request; not in the mutation job.
- The harness's counters and the budget check are tested in
  `domainkit_tests/test_benchmarks.py`, which the test jobs run.

## Consequences

- An extra query, transaction, checkout or ping on any rung fails CI, and
  so does code that grows a path by more than 2% (about 40 calls on the
  template stack: the refused-credential hold check of 0.6.1 added 56).
- Smaller growth and slower code with the same calls pass the gate; the
  summary's timings are there for a reviewer to see them.
- Budgets hold for the CI's Python and the lock file: a dependency upgrade
  that changes FastAPI's or SQLAlchemy's calls moves them, and is measured
  and re-budgeted in the same change (`--write-budgets`, ADR 0065).
- The JSON-log rungs vary by under 1% between runs: how often the logging
  thread waits on an empty queue depends on timing. Their budgets are the
  highest of several runs; the 2% tolerance covers the rest. Every other
  rung counts the same calls in every run.
- The ladder adds about three minutes of CI to each pull request, twice
  the ladder's own time when the base branch is measured too.
- The coordinator of each performance change lowers the budgets it beats;
  a raised budget is a reviewed decision in the diff of `budgets.toml`.
