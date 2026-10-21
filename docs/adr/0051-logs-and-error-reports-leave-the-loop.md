# 0051. Write logs and error reports from threads, dropping what does not fit

- Status: Accepted
- Date: 2026-10-20

## Context

`build_logging_config` wrote every record with a `StreamHandler` on the
calling thread, and `report_error` called each reporter inline. On the
event loop that means a write to stdout, a file, or a socket, and a
tracker's work (Sentry serializes the stack and its locals), in the middle
of a request. When the log collector stops reading the pipe, or a tracker
answers slowly, every request on the loop stops with it (the re-audit's
ASYNC-10, R06). The 0.6 audit measured the JSON access log alone at about
50 µs a request.

Options considered:

- **Keep writing inline, and tell projects to log less.** The stall comes
  from the stream, not the volume: one blocked write stops the loop.
- **An unbounded queue and a thread.** The loop never waits, but a stream
  that stays blocked lets the queue grow until the process runs out of
  memory, which is worse than losing log lines.
- **A bounded queue that blocks when full.** Back to stalling the loop
  under the same conditions, only later.
- **A bounded queue that drops when full, and counts.** The loop never
  waits and memory stays bounded; what is lost is said, in the log, once
  there is room again.

## Decision

- `domainkit.log.BackgroundHandler`, a `QueueHandler`, puts each record on
  a bounded queue (10,000 by default) and returns; one daemon thread,
  `BackgroundListener`, writes through the real handlers. On a full queue
  the record is dropped and counted, and the next record that fits is
  preceded by a warning with the count. `build_logging_config` uses it;
  `queue_size=None` keeps the old, synchronous handler.
- The caller merges the message (later changes to the arguments must not
  show) and runs the filters, since the request and trace ids live in its
  context. Formatting, exceptions included, happens on the thread: reading
  source lines for a traceback is file I/O.
- Error reporters run on one daemon thread from a bounded queue of 100,
  each in a copy of the caller's context, so a tracker still sees the
  request's scope. A full queue drops and counts the same way.
- Shutdown (`close_resources`, after the other resources) waits for both
  queues to drain within its 5 seconds; Python's logging shutdown waits
  for the log queue at exit, and an `atexit` hook for the reports. A
  stream or tracker that stays stuck is given up on.
- A closed handler (at exit, or replaced by another `dictConfig`) writes on
  the caller's thread again rather than queueing records nobody reads. A
  forked child starts with a fresh queue and no thread.

## Consequences

- A blocked stdout or a slow tracker no longer stops requests; under a
  sustained flood, lines and reports are lost, and the count says so.
- Lines appear a moment after the call, from another thread: tests flush
  (`flush_logs`, `flush_reports`; the pytest plugin does it after every
  test) before reading output or checking reporters.
- A crash that kills the process outright (`SIGKILL`, `os._exit`) loses
  what was queued; `queue_size=None` trades that back for blocking.
- Reports for 500s are sent after the response, not before it.

## Addendum (2026-10-21): after the security review

- **Forks.** The after-fork hook took the lock that guards the list of
  handlers. A thread of the parent that held it at the fork never lets go
  in the child, so the child hung before running anything. The lock is now
  held across every fork (`os.register_at_fork(before=...,
  after_in_parent=...)`), so no thread is changing the list then, and the
  child makes a new lock before reading it, as `logging` does with its own.
  A fork waits, at most, for a thread to finish adding a handler or copying
  the list. The lock is reentrant, as `logging`'s is, so a fork from a
  signal handler that interrupted the thread holding it does not wait for
  itself.
