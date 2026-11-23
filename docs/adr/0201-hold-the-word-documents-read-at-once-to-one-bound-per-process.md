# 0201. Hold the Word documents read at once to one bound per process

- Status: Accepted
- Date: 2026-11-23

## Context

A project reported (user report U7, row 2) that `placeholders` and
`fill_docx` bound one call, but not the calls at once.
[0181](0181-decide-a-body-limit-per-request-count-before-the-rules-hand-over-the-file-received-and-bound-the-xml-parsed.md)
bounds the XML one call parses: 340 MiB at most at the default
`ArchiveLimits`. A service runs them in threads, as their docstrings
said (`asyncio.to_thread`), and the loop's default executor runs up to
`min(32, os.cpu_count() + 4)` at once [1]: some 11 GiB in a worker of
eight cores. The documents page showed a limiter for a project to
write, an anyio `CapacityLimiter`, and said that domainkit adds none.
The project wrote one per running loop (`DOCX_AT_ONCE`, 2), for an
upload's `placeholders` and a preview's `fill_docx`.

### What was found

- Memory is the process's. A process may run several event loops, one
  per thread: a server that runs a loop per thread, threads that start
  their own (`domainkit.utils.loops.PerLoop` keeps state for them). An
  anyio `CapacityLimiter` belongs to the loop that uses it, so a bound
  per loop is a bound times the loops. Granian runs one loop per worker,
  its runtime threads being Rust's [2].
- A thread blocked on a semaphore holds a thread of the loop's default
  executor, which also resolves host names (`getaddrinfo`) and runs
  every `asyncio.to_thread`: calls waiting there starve the rest.
- A call that has not started in a `ThreadPoolExecutor` can be
  cancelled; one that runs cannot [1]. `loop.run_in_executor` cancels
  the call when the awaiting task is cancelled [3].
- The password hashers bound their work the same way: one pool of
  `AUTH.HASHING_THREADS` threads per process, which every loop's
  `amake_password` and `acheck_password` wait on. Django names the async
  form of a function with an `a` first (`aget`, `acheck_password`) [4].
- `fill_docx` and `placeholders` already parse in a thread of their own,
  which ends with the call, so that lxml's names go with it (ADR 0181):
  a thread a pool keeps would grow by every name it parsed.

### Options

| Option | What it costs |
| --- | --- |
| Async forms that take `limiter=` | The project still makes the limiter, and holds one per loop; with none given, nothing is bounded. |
| A `DOCUMENTS` setting, an anyio `CapacityLimiter` per running loop | A process of several loops reads that many times the bound at once. |
| A `threading.BoundedSemaphore` in the sync functions | A waiting call holds a thread of the default executor, and cannot leave the queue when its request is gone. |
| **A `DOCUMENTS` setting, `DOCX_AT_ONCE`; async forms, `afill_docx` and `aplaceholders`, that run the sync function on one pool of that many threads per process, which every loop's calls wait on** | A thread of the pool waits for each call's own thread: two threads a call, as `asyncio.to_thread` would take. The pool is kept per size and process (`functools.cache`), as `hashing_pool` is. |

What `DOCX_AT_ONCE` defaults to:

| Option | What it costs |
| --- | --- |
| The number of cores | Up to 340 MiB a core at the default bounds. |
| **2, as the project chose** | 680 MiB at most at the default bounds; a third call waits. |

## Decision

- `DOCUMENTS["DOCX_AT_ONCE"]` (New in 2.10): the Word documents
  `afill_docx` and `aplaceholders` read at once in a process, every
  loop's and thread's calls together; at least 1, at most 64, default 2.
- `afill_docx(document, values, ...)` and `aplaceholders(document, ...)`
  (`domainkit.documents`, New in 2.10) take what the sync forms take and
  raise what they raise. Each runs the sync function on one of the
  process's `DOCX_AT_ONCE` threads (`_readers`, made under a lock, kept
  per size and process id, so a process forked from another makes its
  own); the others wait in the pool's queue, holding no thread. A call
  cancelled while it waits never runs; one cancelled while it runs keeps
  its place until it ends.
- `fill_docx` and `placeholders` are unchanged, and count against no
  bound: a call in a thread of the project's own is the project's.
- `_readers` joins `CACHED` in `test_module_state.py`, beside
  `hashing_pool`: live objects kept for the process, made from settings.

## Consequences

- A service that awaits the async forms reads at most `DOCX_AT_ONCE`
  documents at once per process, whatever runs its loops. Eight fills at
  once of a LibreOffice document of 3.4 MB of XML took 344 MiB with
  `asyncio.to_thread`, and 126 MiB with `afill_docx` at the default:
  2.2 s where `asyncio.to_thread` took 1.9 s, as the work holds the GIL
  between lxml's parses.
- A test that changes `DOCX_AT_ONCE` gets a pool of that size; the pool
  of the old size stays, its threads idle.
- A service whose memory allows more raises `DOCX_AT_ONCE`, or the
  `ArchiveLimits` it passes, knowing each call takes up to 340 bytes a
  node it allows.

## Sources

1. Python, `concurrent.futures.ThreadPoolExecutor` and `Future.cancel`: <https://docs.python.org/3/library/concurrent.futures.html>
2. Granian, README (runtime threads, and one AsyncIO event loop per worker): <https://github.com/emmett-framework/granian>
3. Python, `loop.run_in_executor`: <https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.run_in_executor>
4. Django, Asynchronous support (async forms named with an `a` first): <https://docs.djangoproject.com/en/5.2/topics/async/>
