# 0046. Shut down in phases within one budget

- Status: Accepted
- Date: 2026-10-20

## Context

Shutdown gave each kind of resource 5 seconds in turn: caches, databases,
ledgers, brokers, 20 seconds in all, plus the publisher's own 5 seconds.
Domain shutdown hooks had no limit at all. Inside a kind, items closed one
after another, so one hung database spent the kind's 5 seconds and left
the others open. Nothing outside `lifecycle.py` could add a closer, so
publishers, which management commands never closed, outlived the
connections they used.

Under Kubernetes, the pod gets SIGTERM, then SIGKILL after
`terminationGracePeriodSeconds` (30 by default). The server first waits for
requests in flight (uvicorn: without limit unless
`--timeout-graceful-shutdown` is set), then runs the lifespan's shutdown.
What runs after SIGKILL does not run: connections are dropped instead of
closed, and messages a publisher held stay for the relay.

Options considered:

- **Per-step timeouts, summed.** Simple, but the sum is what operators must
  fit into the grace period, and it grows with every step added.
- **One deadline, first come first served.** A hung domain hook would take
  the time the database closes needed.
- **One deadline, split into phases with guaranteed shares.**

## Decision

- `SHUTDOWN_TIMEOUT` (15 seconds by default, at most 300) is the whole
  budget. With `--timeout-graceful-shutdown 10` that fits a 30-second grace
  period with 5 seconds to spare.
- Shutdown runs five phases in order: `domains` (domain hooks), `drain`
  (work in flight), `tasks` (owned tasks, ADR 0045), `resources`, and
  `flush`. Each ends at a fixed share of the budget counted from the start:
  30, 50, 60, 90, and 100%. Time a phase does not use carries forward, and
  no phase can take another's share.
- Within a phase, closers run concurrently and are bounded by the phase's
  end; one still running then is cancelled, logged, and left behind, even
  one that ignores cancellation. Items of a kind close concurrently too
  (`close_each`).
- Domain hooks must keep their reverse order, so they run in turn, each
  within a fair share of what is left of the phase (`close_in_turn`); one
  that overruns is cut off with a `TimeoutError` naming it.
- `lifecycle.register(name, close, phase=...)` adds closers to `drain`,
  `resources`, or `flush`. Management commands run the same phases, except
  `domains`, through `close_resources`.

## Consequences

- Operators reason about one number, plus the server's graceful shutdown.
- A hung dependency costs at most its phase's share; everything else still
  closes cleanly.
- Leaving a closer running after its phase means it may still be working
  while later phases run; it has been cancelled and will end at its next
  wait.
- The shares are fixed in code. A phase that needs longer gets it by a
  larger budget, not by taking from another; other work packages (the
  publisher flush, the HTTP client, log flushing) choose their phase and
  must finish within its share.
