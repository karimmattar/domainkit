# 0051. Write logs and error reports from threads, dropping what does not fit

- Status: Accepted; amended 2026-10-21, 2026-10-26 and 2026-11-17 (at the end)
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
- **Room for warnings.** A stalled stream let access lines fill the queue,
  and a `domainkit.security` warning was then dropped like any line, with
  only a count to say so. Records below `WARNING` now fill at most 90% of
  the queue (`WARNING_RESERVE`); warnings and errors may use the rest. A
  tenth keeps 1,000 places of the default queue for them, more than a
  burst of refusals needs, while lines below lose only a tenth of their
  room; a queue of fewer than 10 places keeps none, so a tiny queue still
  takes every level. The drop notice counts by level (`Dropped 52 log
  records (50 INFO, 2 WARNING)`), and goes ahead of a record only when
  both fit, so it never takes the place of the warning it would precede.
- **One wait at exit.** `logging.shutdown` calls the handler's `flush()`
  and then its `close()`, and each step inside them (room for the drop
  notice, room for the marker, the thread reaching it, ending the thread)
  waited its own 5 seconds: on a stuck stream, exit took 15 seconds. The
  steps of each call now share one deadline, `close()` right after a
  `flush()` gets only what is left of the flush's, and none at all once the
  flush gave up: exit waits `FLUSH_TIMEOUT` at most, as this record says.
  The thread, a daemon, still writes what it can while the process lives.

## Amendment (2026-10-26, 0.10): exceptions are formatted before they are queued

A queued record kept its `exc_info`, to be formatted on the thread, as
this record decided. A traceback refers to the frames that raised and
their locals (a request, its session, its body), so with a slow output up
to a full queue of them kept requests alive (CONC-04 of the 0.9.0 audit).
`BackgroundHandler.prepare` now formats the exception on the caller, as
the standard library's `QueueHandler` does, into `exc_text`, which every
formatter writes as it wrote the traceback, and the queued copy keeps no
`exc_info`. Measured with a stuck output and 2,000 queued errors from
frames holding 50 kB each: 103.1 MB kept before, 1.7 MB after. The caller
pays the formatting, on errors only: 59 µs before, 176 µs after, per
record. A handler behind the queue that needs the exception object, not
its text, no longer gets it; error trackers take exceptions from
`ERROR_REPORTERS`, which this does not change.

## Amendment (2026-11-17, 2.6): a fork waits for the writes in progress

A process forked while the logging thread wrote to a buffered stream
(standard output on a pipe, a file) inherited the stream's lock held, by
a thread the child does not have. The child's first write from its main
thread waited forever, and its own logging thread's lines were lost with
`reentrant call` errors. 2.5.0 spared `manage.py serve` by spawning
Granian's workers; `gunicorn --preload`, `multiprocessing`'s fork, and a
project's own `os.fork` still hit it: in the test, children forked while
the thread wrote hung (up to 19 of 20) or lost their lines.

So a fork now waits for the writes in progress too, not only for a
thread changing the list of handlers. `logging` has each handler write
under its own lock. The hook before a fork asks every running logging
thread to wait at its turnstile before its next record, takes the
turnstiles, then each handler's lock (a thread's, below) as its write
ends. The thread itself takes no lock per record, and passes the
turnstile only while a fork asks: no call more per record than before.
Without the turnstile, a thread took its handler's lock again for its
next record before the fork woke. The waits end `FLUSH_TIMEOUT` after
the fork has `logging`'s lock: a write still going on then is on a stuck
stream, which the child could not write to either, and the fork goes on
without it.

The hook takes `logging`'s lock first, as `logging`'s own hook does,
waiting while it holds nothing, and the lock of the handler list last.
`dictConfig` takes `logging`'s lock, then ours as it makes handlers, and
the old handlers' locks as it flushes them, waiting for their threads.
A fork that took ours before `logging`'s deadlocked against it: a loop
of forks against a loop of `dictConfig` calls did within seconds. A fork
that held the threads while it waited for `logging`'s lock made a
`dictConfig` that flushed a running handler wait `FLUSH_TIMEOUT`, and
one that held a handler's lock while code on its own thread (a signal
handler that logs, a collected handler's callback) waited for
`logging`'s lock deadlocked against it. With `logging`'s lock first, a
`dictConfig` under way finishes before the fork holds anything, and
code on the forking thread takes the lock again. The price: other
threads that need `logging`'s lock wait for the fork's wait for the
writes, one write or `FLUSH_TIMEOUT` on a stuck stream, and a handler
that takes `logging`'s lock in the middle of a write holds the fork
`FLUSH_TIMEOUT` and is then left (it is not writing to a stream then).
Such a handler deadlocks against `dictConfig` in plain `logging`. We
weighed keeping the threads first and never waiting while holding a
handler's lock (try each, let all go to wait for a busy one, retry, the
collector off during the hook): retries, and a process-wide switch in a
fork hook, to spare handlers that take `logging`'s lock while they
write, which plain `logging` does not support either.

In the child, the hook renews what the fork took. `logging` renews only
the locks its own `createLock` made; a handler that makes its own kept
it held by the child's only thread, and the child's logging thread
waited for it at its first record. The fork takes only the locks it can
renew, `threading`'s. A lock shared between processes (a
`multiprocessing` one) taken at the fork was the child's too, its owner
being the forking thread: the child took it again without waiting while
the parent let it go, and both wrote at once. Such a lock is not waited
for: a write in progress under it is the child's to wait for, as any
other process's.

Each thread keeps what its forks hold apart, noted as each lock is
taken, and takes no lock twice. A signal handler may fork while its
thread's fork runs its hooks, and each fork lets go of what it took;
one that raises while the hook waits (Ctrl-C) leaves nothing held, and
Python forks all the same. One that raises in the instant after a lock
is taken and before it is noted, a few bytecodes after the wait, leaves
that lock held.
