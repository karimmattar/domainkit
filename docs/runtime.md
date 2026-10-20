# How domainkit runs async work

What keeps the event loop free: which loop runs, how logs and error reports
leave it, how blocking code is found before production, and which servers
and Python builds domainkit runs on.

## Event loops: uvloop

`runserver` and every management command with an `async` handle
(`runworker`, `migrate`, `seed`, ...) run on [uvloop](https://github.com/MagicStack/uvloop)
when it is installed: `pip install 'domainkit[uvloop]'` (uvicorn's
`standard` extra brings it too, on Linux and macOS). `EVENT_LOOP` chooses:

| `EVENT_LOOP` | Loop |
| --- | --- |
| `"auto"` (default) | uvloop when it can be imported, asyncio's own otherwise |
| `"asyncio"` | always asyncio's own: rule uvloop out when chasing a bug |
| `"uvloop"` | uvloop, or a configuration error when it is missing |

Production servers pick their own loop: uvicorn and Granian take
`--loop uvloop` (both default to `auto`). Code that starts a loop of its own
can follow the setting:

```python
import asyncio

from domainkit.conf import settings
from domainkit.utils.loops import loop_factory

asyncio.run(main(), loop_factory=loop_factory(settings.EVENT_LOOP))
```

uvloop does not run on Windows; `auto` falls back to asyncio's loop there,
and the extra installs nothing.

## Logging and error reports

Writing a log line can block: stdout is a pipe a log collector may stop
reading, and a file or socket can stall. A handler called on the event loop
would then stop every request. `build_logging_config` writes through a
`domainkit.log.BackgroundHandler` instead, and the project settings `init`
writes use it:

```python
from domainkit.log import build_logging_config

LOGGING = build_logging_config(log_format="json", level="INFO")
```

- The caller, the event loop included, merges the message, adds the
  request id and trace ids (they live in its context), and puts the record
  on a queue of 10,000 (`queue_size`). One thread, `domainkit-logging`,
  formats and writes it; exceptions are formatted there too.
- A full queue never makes the caller wait. The record is dropped and
  counted (`handler.dropped`), and the next record that fits is preceded by
  a warning from `domainkit.log`: `Dropped 120 log records: the logging
  queue was full`.
- At shutdown the lifespan and management commands wait for the queue to
  drain, within 5 seconds, after closing the other resources; Python's own
  logging shutdown does the same at exit. A stream that stays blocked is
  given up on, so it cannot hold the process.
- `queue_size=None` writes on the caller's thread, as plain handlers do:
  for scripts that must see each line before going on.

Another `LOGGING` can use the handler around any handlers:

```python
LOGGING = {
    "version": 1,
    "handlers": {
        "file": {"class": "logging.FileHandler", "filename": "app.log"},
        "background": {
            "class": "domainkit.log.BackgroundHandler",
            "handlers": ["file"],
            "queue": {"()": "queue.Queue", "maxsize": 10000},
            "listener": "domainkit.log.BackgroundListener",
            "filters": ["context"],
        },
    },
    "filters": {"context": {"()": "domainkit.log.ContextFilter"}},
    "root": {"handlers": ["background"], "level": "INFO"},
}
```

Error reporters (`ERROR_REPORTERS`) run off the loop too, on one
`domainkit-reporting` thread: `report_error` queues the report and returns.
Each reporter runs in a copy of the caller's context, so Sentry still sees
the request's scope and log lines keep its request id. At most 100 reports
wait; more are dropped and counted, and the count is logged. Shutdown waits
for waiting reports, within 5 seconds, before the logs. In tests, call
`domainkit.reporting.flush_reports()` before checking what was reported
([testing.md](testing.md#fixtures)).
