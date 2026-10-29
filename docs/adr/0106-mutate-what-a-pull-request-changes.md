# 0106. Mutate what a pull request changes, before it merges

- Status: Accepted; amended 2026-10-29 (at the end)
- Date: 2026-10-28

## Context

Coverage is 100% of lines and branches, yet the 0.9.0 audit found modules
whose behaviour no test pins (TEST-07): a line can run without any test
noticing what it does. Mutation testing finds them: mutmut changes the
code one small edit at a time (a mutant) and runs the tests that reach it;
a mutant no test fails is behaviour no test pins. CI ran it weekly, in 17
shards of the whole package (INFRA-01), and TEST-02 asked for a gate on
pull requests too, so a change is not merged a week before anyone learns
what its tests miss.

Measured on this repository before deciding:

- **The weekly run did not work.** Its last run (2026-10-26) stopped in
  every shard about two minutes in, before any mutant: mutmut first runs
  the whole selected suite on its rewritten copy of the package, every
  function wrapped in a trampoline, to learn which tests reach which
  function, and stops at the first failure (`-x`). Seven tests fail on
  that copy: one reads domainkit's source text (`scope["path"]` appears
  once per mutant of each function there), three check the frame a log
  record names as its caller (the trampoline's), and three start a
  generated project's interpreter, which inherits `MUTANT_UNDER_TEST` and
  looks for mutmut's settings in the project. Contributors' runs worked
  because they narrowed the tests to a few files.
- **What a run costs.** mutmut's fixed cost is generating the mutants of
  the whole package (47 s), then the suite twice in one process, once
  tracing which tests reach which function (14 minutes here) and once
  clean (8 minutes), where the test jobs take 4 minutes on four workers.
  Each mutant then runs only the tests that reach its function, on every
  core. The functions the fixture work of 0.12 changed (16 functions in
  four modules, 408 mutants) took 6 minutes of mutants, 29 minutes in
  all, on four cores.
- **How it scales.** The fixed cost, about 23 minutes, does not grow with
  the change; the mutants do, with the number and size of the functions
  changed, at about a second each here. The four modules whole have 1,247
  mutants, three times the changed functions' 408.

The options for a pull request:

- every module the pull request changes, whole: a change to one function
  of `fixtures.py` mutates the 39 others, and the run is longer for big
  modules than the change warrants;
- the functions and methods whose lines the change adds or edits, by the
  names mutmut gives their mutants (`module.x_name`,
  `module.xǁClassǁname`), from the diff against the base branch;
- narrowing the tests mutmut runs too: the stats run would then miss the
  tests elsewhere that reach a changed function, and their mutants would
  read "no tests".

And for what the job fails on: a score threshold (an arbitrary number, and
a change of one function scores 0% or 100%); any mutant that lives; or
only a run that does not finish.

## Decision

- **The weekly run works again.** The four tests that read domainkit's
  source or frames carry the marker `reads_own_source`, and
  `[tool.mutmut] pytest_add_cli_args = ["-m", "not reads_own_source"]`
  leaves them out of mutation runs only: they check text and frames, not
  behaviour a mutant changes, and every test job still runs them. The
  generated projects start without mutmut's variable, as the walkthroughs
  already did.
- **`mutation (changed)`**, a job on every pull request that changes code,
  mutates the functions and methods the change touches
  (`.github/scripts/changed_mutants.py HEAD^1`, on the merge commit), with
  the whole suite selected, so every test that reaches them counts.
  mutmut is stopped at 45 minutes (the job has 65), which leaves the
  mutants about 20 minutes, some 1,000 at this repository's pace, and still
  reports.
- **It fails** when the run does not finish, or when a changed function's
  mutant lives (`mutation_summary.py --lived-fails`), listing each in the
  job summary and in an artifact: the author kills it with a test, or says
  in the pull request why it stays (an equivalent mutant), as
  `CONTRIBUTING.md` already asks.
- **It is not required.** The ruleset does not name it (`test_ci.py`'s
  `ADVISORY`): an equivalent mutant cannot be killed, and requiring the
  check would make each such pull request an admin's bypass. Whether to
  require it is the owner's choice, once the weekly runs show how often
  equivalent mutants live in changed code.

## Consequences

- A pull request shows the mutation score of what it changed within the
  hour, where it showed none.
- Every pull request that changes a function pays the fixed cost, about
  23 minutes of runner time, even for a one-line change; one that changes
  only docs, tests, or no function pays nothing beyond finding that out.
- A change too large for the budget reports incomplete and fails, naming
  the mutants never checked: split it, or read the weekly run.
- A test that reads domainkit's source or frames must carry
  `reads_own_source`, or the weekly run and this job stop again; the
  failure names the test.
- What would change this: mutmut learning which tests reach a function
  without a full run (a coverage map kept from the test jobs), which would
  cut the fixed cost to the mutants' own.

## Amendment (2026-10-29, 0.13): what the first full weekly run showed

The run on 0.12.0 (CI run 40846333116) reported 3,078 mutants that lived
and 318 no test reaches, over 15 of its 17 shards. Three things in it
were the run's, not missing tests:

- **Functions behind a cache.** The stats run maps each function to the
  tests that call it, and a function behind `functools.cache` runs for
  the first test that needs it only, with everything it calls: a cached
  statement builder's helper was mapped to 6 tests, and a mutant giving a
  user every other user's groups lived, though the suite kills it at
  once. Under the stats run (`MUTANT_UNDER_TEST=stats`), the tests now
  empty domainkit's caches before each test (`domainkit_tests/conftest.py`):
  the helper is mapped to 332 tests, and 982 functions to more than
  before.
- **Code only a server reaches.** The shards run on SQLite, so what only
  PostgreSQL, Redis, immudb, or RabbitMQ runs is "no tests" or lives
  (`db.connections`, `db.partitioning`, `ledger.immudb`, `cache.redis`,
  the worker's `LISTEN`). Those mutants say nothing of the tests.
- **A shard that stops.** One flaky test in a shard's stats run ends the
  shard before its first mutant (`auth-4`; it ran in full on another
  machine, see `docs/audit/0.9.0.md`, TEST-07), and a lost runner ends it
  with nothing reported (`management-4`).

The weekly run still reports what it did; the audit page lists, by
module, what the 0.12.0 run left and why.

