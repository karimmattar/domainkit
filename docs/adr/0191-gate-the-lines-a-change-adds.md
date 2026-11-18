# 0191. Gate the lines a change adds

- Status: Accepted
- Date: 2026-11-18
- Amends: [0106](0106-mutate-what-a-pull-request-changes.md)

## Context

The 2.8 audit (TEST-04, TEST-05) read the gates on test strength against
what the docs said of them.

- **Coverage.** Since 1.5.0 (commit `30bf623`), `fail_under = 98` in
  `pyproject.toml`, CI's `coverage` job and `make cov` hold every run, and
  the runs combined, to 98%. README.md and the Makefile still said 100%.
  `CONTRIBUTING.md` said new code is covered 100%, branches included, which
  nothing checked. On 2.8.0 the package had 77,558 units (statements and
  branches): 98% lets 1,551 go unrun, and both runs combined missed 272,
  about 190 of them outside the modules only a server reaches. Some 1,300
  units of new, untested code fit under the gate.
- **Mutation testing.** Since the same commit, `mutation (changed)` and the
  weekly shards run only while the repository variable `MUTATION` is `on`,
  and it has stayed off: "coverage gates every change". ADR 0106 and
  `CONTRIBUTING.md` still said every pull request ran its mutants.
  `docs/roadmap.md` says: "Mutation testing stays off by decision; coverage
  stays at 98% or more."
- **Subprocesses.** Code that only runs in a Python process a test starts
  counted as missed: the sorting of sets in `contrib/auth/actions.py`
  (`_canonical`) is tested in six interpreters of different hash seeds,
  and showed as never run. A gate on the lines a change adds would refuse
  such a change.

### Options

1. **100% of the package again.** Every run on both databases must first
   test the 190 units both miss, and a line a newer Python no longer
   reaches fails every pull request at once, whatever it changed.
2. **diff-cover** compares a coverage report with the diff and fails below
   a threshold. It is not in the dev group, and 2.9 adds no dependencies.
3. **A short script over the diff and coverage's own JSON report**
   (chosen): the rule diff-cover applies with `--fail-under=100`, in about
   a hundred lines, tested beside the other CI scripts.
4. **Correct the docs, and keep 98% alone.** The slack stays.

Turning `MUTATION` on was weighed too. ADR 0106's 0.15 amendment found
the check red on the pull requests of four releases, at about 23 minutes
of runner time each, and a survivor on SQLite says nothing of a missing
test until it is run on PostgreSQL. Google found coverage most useful when
it is applied to each change in code review [1]; diff-cover gates the
same way [2].

## Decision

- The package stays at 98% in every run, and in the runs combined
  (`pyproject.toml`, CI's `coverage` job, `make cov`).
- On a pull request to `stg`, the `coverage` job fails when a line the
  pull request adds or changes under `src/` ran in no job, or a branch
  from it was never taken: `.github/scripts/changed_coverage.py HEAD^1`
  on the merge commit, against `coverage json` of every run combined.
  Not on the release's pull request from `stg` to `main`: its diff is the
  whole release, whose lines were held to this as they reached `stg`; a
  release may run `make cov BASE=origin/main` by hand. `make cov` runs
  the check against `origin/stg` (`BASE=` names another).
- A line no CI job can run (another platform; the free-threaded build,
  which gathers no coverage) carries `# pragma: no cover` and the reason,
  for the review to read.
- Python processes the tests start are measured, when they keep the
  environment: `[tool.coverage.run] patch = ["subprocess"]`, which
  coverage has from 7.10 and which sets `parallel` [3]. pytest-cov 7.1,
  the floor, requires coverage 7.10.6. A test that counts what a fresh
  interpreter loads leaves coverage out of its child (`test_lazy_imports`).
- Mutation testing stays on request, and the docs say so (ADR 0106's
  amendment of 2026-11-18).

## Consequences

- A pull request can no longer add untested code under the 98%. One that
  edits an old untested line tests it: the line counts as changed.
- A line that only runs in a process that does not keep the environment
  (an upload check's own process, by design) counts as missed: test it in
  the test's process too, or mark it.
- Measuring subprocesses costs time where a test starts interpreters:
  about two to two and a half times on the end-to-end tests
  (`test_proof.py` 22.5 s to 55.4 s, `test_init.py` 14.2 s to 26.4 s, in
  review). Those tests are a minority of the suite, but its longest.
  The `test` job's suite took 14 to 26 minutes on hosted runners once
  they were measured, and one run of 3.15 met the 30-minute limit at 99%:
  the job has 45 minutes, as integration's has.
- The gate can refuse a change for a line that ran in no CI job for a
  reason other than a missing test: a branch only a timing reaches, an
  old line partly covered that the change edits (it counts as added), or
  a line only `test (3.14t)`, `newest` or `oldest` runs, which gather no
  coverage. Such a line is tested, or carries `# pragma: no cover` and
  its reason.
- What would change this: diff-cover in the dev group, which would replace
  the script; `MUTATION` turned on, which would add a second check of the
  changed lines.

[1] Ivanković, Petrović, Just, Fraser, "Code coverage at Google",
ESEC/FSE 2019: <https://research.google/pubs/code-coverage-at-google/>.

[2] diff-cover: <https://github.com/Bachmann1234/diff_cover>.

[3] coverage.py, "Measuring subprocesses" and `[run] patch`:
<https://coverage.readthedocs.io/en/latest/subprocess.html>,
<https://coverage.readthedocs.io/en/latest/config.html#config-run-patch>.
