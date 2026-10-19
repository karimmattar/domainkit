# 0037. Read the time from one clock, frozen per context in tests

- Status: Accepted
- Date: 2026-10-19

## Context

Nineteen modules of the framework read the time on their own:
`datetime.now(UTC)` for token expiry, idempotency leases, outbox and audit
timestamps, events, evidence, and partition maintenance; `time.monotonic()`
for rate windows and cache lifetimes. Tests that cared about time patched
one of them (`monkeypatch.setattr(idempotency, "_now", ...)`), so a test of
"the token has expired" had to know which module asks, and a project's own
tests had no way in at all. JWTs were worse: PyJWT checks `exp`, `iat`, and
`nbf` against the host's clock, which nothing can move.

Options considered:

- **freezegun or time-machine**, which patch `datetime` and `time`
  process-wide. Every thread and library moves, including drivers,
  pools, and timeouts that must not; and it is a new dependency.
- **A clock port** passed to each component. Clean, but it changes dozens of
  signatures, and code that is not handed the clock still reads the host's.
- **One module the framework reads the time from**, whose readings a
  context-local override can replace.

## Decision

- `domainkit.clock` is the framework's only source of the time: `now()`
  (aware, UTC), `today()` (UTC), and `monotonic()`. Framework code calls
  them; helpers such as `contrib.auth.tokens.utc_now` stay, and call them.
- `freeze_time(when, tick=False)` returns a `FrozenClock`. Entered, it is
  the clock of the current context: a `ContextVar`, as the request context
  is. Tasks started inside see it; concurrent requests and other tests do
  not. `tick()` and `move_to()` move it for every context using it. A
  stopped clock is skipped wherever a copy of the context still holds it,
  so a clock started in an async test's task, and stopped from pytest's
  thread, cannot leak into the next test.
- `monotonic()` under a frozen clock stands still with it and moves forward
  with it, never back: rate windows and cache lifetimes follow, and no
  interval measured inside a freeze is negative.
- JWTs are verified by PyJWT, but their times are checked by domainkit, as
  PyJWT would check them, on `domainkit.clock`.
- The domain layer may import `domainkit.clock` (it is as pure as the
  standard library), so aggregates can stamp times that tests freeze.
- The database's clock stays the database's: `now()` in SQL, a column's
  `server_default`, and Redis's `TIME` in shared rate limits are not
  frozen. Operational timers (readiness caching, worker housekeeping,
  outage cooldowns) also keep real time.

## Consequences

- A test freezes time once (`with freeze_time(...)`, or the plugin's
  `freeze_time` fixture) and tokens, leases, rate windows, cache lifetimes,
  and audit and event timestamps all agree.
- The sync `TestClient` runs the application in another thread; domainkit's
  `TestClient` copies the caller's context values into each request so the
  freeze reaches it. Starlette's own `TestClient` does not.
- Framework code must call `domainkit.clock`, never `datetime.now()` or
  `time.monotonic()`, for anything a test may want to move; a review rule,
  not a lint.
- Time that comes from the database is not frozen. Tests that mix the two
  (a `server_default=func.now()` column compared with `clock.now()`) must
  freeze the database's side themselves, or avoid comparing them.
- Reading the time costs one `ContextVar` lookup more.
