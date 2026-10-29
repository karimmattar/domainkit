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

## Amendment (2026-10-29, 0.12.1): shards for a release-size change

The job was sized at 408 mutants in 6 minutes. 0.12's release pull request
(#86) changed 176 functions, 3,247 mutants, and the job stopped at its
45-minute limit: 1,660 killed, 72 survived, 204 timed out, and 1,311 never
reached. It failed as it should, naming them, but no one learned what the
1,311 hid (TEST-02).

Measured on this repository (four workers, as GitHub's runners have), with
the 3,095 mutants of those functions outside `batching.py`:

- **The fixed cost.** Generating the mutants: 75 seconds. mutmut's stats,
  the whole suite in one process under the trampolines: 16 minutes. Its
  clean run, of the tests that reach the selected functions (for 176
  functions, nearly the whole suite), in one process: 15 minutes. So a
  release-size change has about 9 minutes of a 40-minute budget left for
  its mutants.
- **The mutants' cost** is not a second each. mutmut runs the cheapest
  first. The first 2,189 took 53 minutes; the last 906, the mutants of
  functions that every domain's setup reaches (`HookPoint`, the domains'
  `ready`, `UnitOfWork.commit`, the idempotency middleware), took 118
  minutes (other runs shared the machine for part of it): each runs, at
  worst, tests of 140 to 260 seconds. In all 170 minutes of mutants, 204
  with the fixed cost; 2,498 killed, 456 timed out, 141 survived. A
  worker's time per mutant fits 4 seconds, plus 0.27 (the cheap ones) to
  0.33 (the heavy ones) of its tests' time as mutmut's stats give it: most
  mutants fail a test early, and mutmut stops there.
- **The stats can be reused.** mutmut keeps them in
  `mutants/mutmut-stats.json`; a run that finds them, for the same commit
  and configuration, skips the 16 minutes and only lists the tests. Its
  clean run covers only the tests of the mutants it is given.

The options:

- a longer limit for the one job: GitHub allows six hours, so 0.12's
  mutants would finish, in about three and a half hours, and every pull
  request waits as long as its slowest mutants;
- a fixed matrix of shards by module, as the weekly run has: every pull
  request pays every shard's 31 minutes of stats and clean run, for its
  three changed functions;
- one job first, as now, and shards only for what it leaves, made from
  its stats.

### Decision

- `mutation (changed)` runs as before, stopping mutmut at 40 minutes (it
  was 45). A typical pull request finishes there: one job.
- When mutants are left unchecked, `mutation_shards.py plan` splits them
  into shards, balanced by the estimate above (4 seconds and 0.35 of the
  tests' time a mutant), at about 5,000 seconds of a worker's time (21
  minutes on four, besides a clean run of up to 15) each, and 12 shards at
  most. A function's mutants stay together, 100 or half a shard at most,
  since each shard's clean run is the tests of its functions. The job
  uploads the plan and its stats (`mutation-changed-plan`), and its
  results.
- `mutation (changed, N)`, a matrix made from the plan, takes the stats
  and runs its mutants, stopped at 45 minutes (the job has 65).
- `mutation (changed, summary)` reads every run's results, a checked
  mutant winning over one interrupted or not reached, and fails, as the
  single job did, when a mutant lived or was never checked, listing them
  in its summary and an artifact.
- None of them is required (`test_ci.py`'s `ADVISORY`), as before.

### Consequences

- 0.12's pull request would take 12 shards after the first job (its
  3,247 mutants estimate at 64,551 seconds of a worker's time, 269
  minutes on four, where the 3,095 here took 170: the estimate errs
  long): about 45 minutes, then about 45 more, where it stopped
  incomplete. A proof on three
  modules' 889 mutants ran the first job (stopped before its mutants),
  the plan, a shard in a fresh tree with only the plan and the stats (no
  stats run), and the summary over both: complete, 878 killed and 11
  survived, failing as it should.
- A shard repeats its clean run of its functions' tests; a shard of the
  functions every setup reaches pays nearly the whole suite again, about
  15 minutes, inside its 45.
- The estimate is fitted to this suite; when a shard does not finish, its
  mutants read "not checked" in the summary, and the constants in
  `mutation_shards.py` are the place to change.
- mutmut runs its stats and its clean run in a forked child
  (`process_isolation = "forkserver"`) and never shows that child's
  output: the weekly run's `auth-4` shard failed once at its stats with
  only "failed to collect stats. runner returned 1". `conftest.py` writes
  the test that is running, and what failed, to
  `mutants/mutmut-failures.txt` in those runs, and every mutation job
  prints it when mutmut fails before its time is up.
- What would change this: mutmut recording a mutant's duration under
  forkserver (3.8 records zero), which would fit the estimate from each
  run instead.

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

