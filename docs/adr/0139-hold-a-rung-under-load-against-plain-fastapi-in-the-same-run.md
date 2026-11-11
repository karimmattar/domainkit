# 0139. Hold a rung under load against plain FastAPI in the same run

- Status: Accepted
- Date: 2026-11-11

## Context

The benchmark ladder sends one request at a time, straight into the ASGI
application, and gates what it counts: calls and database round trips
([ADR 0064](0064-gate-performance-on-counts-not-time.md)).
That is exact, and it cannot see what only happens with requests in
flight together: a lock every request waits on, a pool that runs dry, a
call that blocks the event loop, a server that drops connections. The
audit of 2.2.1 recorded the gap: nothing in CI served the framework over
sockets, or measured a tail latency (P5).

ADR 0064 kept time out of the gate, for a reason that still holds: on a
shared runner the same code's time spreads by 15% and more, and whole
runs come in slow. A gate on milliseconds either lets regressions
through or fails on nothing. One in four CI runs already failed on a
flaky test when the audit was written; a new check must not add to that.

The options:

- **A load generator and absolute limits** (`oha`, `wrk`, `k6`, a p99 in
  milliseconds): the numbers people ask for, on a machine that cannot
  give them twice. Also a binary to download and pin in CI.
- **Compare with the base branch**, as the ladder reports CPU time: fair,
  but it doubles the run, and a pull request that changes the harness
  compares two different measurements.
- **Compare with a fixed reference served in the same run.** Plain
  FastAPI, on the same uvicorn, on the same machine, in the same minute:
  the ladder already has it as a rung. What slows the machine slows
  both, so a rung's figure divided by the reference's moves far less
  than either. It is not exact: two runs on a quiet 4-core machine put
  the `template` rung at 0.43 and 0.56 of FastAPI's requests a second,
  and its 99th percentile at 2.6 and 2.1 times FastAPI's. Three more
  runs while a test suite kept all four cores busy lost a third of the
  requests a second, and doubled the tail in milliseconds; the ratios
  stayed at 0.43 to 0.52, and 1.8 to 2.4.
- **Count what must never happen**, whatever the speed: an answer that is
  not the rung's status, a connection the server dropped.

## Decision

- `python -m benchmarks.load` serves each chosen rung with uvicorn, in a
  process of its own, and sends it requests over 32 connections kept
  open, a request at a time on each. A warm-up, then a measured stretch,
  three rounds, the rungs alternating. A rung's result is its median
  round: requests a second, the 50th and the 99th percentile.
- The client is the harness's own, on asyncio: no binary to install, and
  nothing to pin. It shares the machine with the servers. The figures
  compare rungs and runs; they do not size a deployment
  (`benchmarks/compare` does, with `oha`).
- **Gated: every answer is the rung's.** One response of another status,
  one lost connection, fails the run and says what came back.
- **Gated, loosely: ratios to plain FastAPI in the same run**, in
  `benchmarks/load.toml`: a rung's 99th percentile at most `p99_ratio`
  times the baseline's, and its requests a second at least
  `throughput_ratio` of the baseline's. The budgets sit about four times
  away from what was measured. They catch a rung that got several times
  slower under load, not one that lost 10%: calls and round trips, which
  are exact, catch that.
- **The database rungs are held on requests a second only.** Their tail
  is the pool's and the server's more than the code's (below), and
  differs from one PostgreSQL to another.
- **No time is gated.** The milliseconds go to the job's summary and to
  an artifact. ADR 0064 stands.
- CI's `bench` job runs it after the ladder, on `fastapi`, `template`,
  `me`, `read` and `write`, against the job's PostgreSQL. `make bench`
  runs it on the rungs without a database.
- The database's URL goes to the servers in their environment, and what
  a failed server printed is reported with the password masked, as the
  ladder does.

## Consequences

- CI now starts the framework under a real server on every run, with
  requests in flight together. A deadlock, a pool that refuses, or a
  blocking call several times the cost of a request fails the job.
- The first runs found something the ladder could not. With 32
  connections on the default pool (5, and 10 of overflow), the `read`
  rung served 481 requests a second with a 99th percentile of 338 ms;
  with the pool at 32 and no overflow, 605 a second and 149 ms. Past
  `POOL_SIZE`, SQLAlchemy closes each overflow connection when it comes
  back, so a busy process connects again and again.
  [performance.md](../performance.md#under-load) records it.
- The check adds about a minute and a half to the `bench` job.
- The ratios are loose, so a slow drift passes. When a budget is far
  from what the job's summary shows over several runs, tighten it; when
  a run fails on a ratio and a rerun passes, widen it and say why in the
  pull request.
- The budgets were written from runs on one machine. The first runs on
  CI's runners may show other ratios: the summary has them.
