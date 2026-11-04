# Observability

Observability is what a running service tells you about itself: its
logs, its metrics, and its traces. domainkit writes logs as JSON lines,
counts requests, messages, and caches for Prometheus, and opens spans for
OpenTelemetry. This page lists what each one holds. Metric and span names
are kept within 1.x where possible ([versioning.md](../versioning.md#what-is-public)).

On this page you find:

- what a log line holds, and what it never holds;
- every metric, with its labels;
- which work gets a span, and how a trace crosses from a request to the
  worker.

## Logs

`domainkit.log.build_logging_config` makes the `LOGGING` setting that the
`init` templates use. With `log_format="json"`, each record is one JSON
line:

```python
import logging
import logging.config

from domainkit.log import build_logging_config

logging.config.dictConfig(build_logging_config(log_format="json", level="INFO"))
logging.getLogger("shop.orders").info("Order placed")
logging.shutdown()
```

```text
{"timestamp": "2026-11-04T18:35:59.822+00:00", "level": "INFO", "logger": "shop.orders", "message": "Order placed"}
```

- Inside a request, a line also holds `request_id`, and `trace_id` and
  `span_id` when a span is active.
- Query strings and bound SQL values are never logged.
- Records are written by a thread, never by the event loop. A burst past
  the queue's 10,000 records is dropped and counted, never waited for
  ([runtime.md](../runtime.md#logging-and-error-reports)).
  `build_logging_config(..., queue_size=None)` writes from each caller
  instead.
- `log_format="text"` writes lines for people, as the local settings do.

The access log (`domainkit.access`) writes one line per request; see
[The access log](http.md#the-access-log) for its settings.

## Metrics

Metrics need the `metrics` extra (`domainkit[metrics]`). The application
serves them at `/metrics`. In staging and production, `/metrics` answers
403 without `METRICS_TOKEN` ([http.md](http.md#metrics)):

```bash
curl -H "Authorization: Bearer $METRICS_TOKEN" https://api.example.com/metrics
```

`runworker --metrics-port 9100` serves the worker's metrics
([commands](commands.md#runworker-run-background-work)).

### HTTP and the database

| Metric | Labels |
| --- | --- |
| `http_requests_total` | `method` (`OTHER` for any but the nine standard ones), `route` (the template, never the raw path), `status` |
| `http_request_duration_seconds` | `method`, `route` |
| `http_requests_in_progress` | `method` |
| `db_pool_connections` | `database`, `state` |
| `http_requests_admitted` | (requests holding a slot under `MAX_CONCURRENT_REQUESTS`) |
| `http_requests_queued` | (requests waiting for a slot) |
| `http_requests_shed_total` | `reason` (`queue_full`, `queue_timeout`) |
| `http_request_deadlines_exceeded_total` | |

### Messaging

| Metric | Labels |
| --- | --- |
| `messaging_messages_total` | `kind`, `outcome` (`done`, `retry`, `dead`, `throttled`, `handled`, `superseded`, `unknown`, `unrecorded`, `interrupted`) |
| `messaging_message_duration_seconds` | `kind` |
| `messaging_outbox_messages` | `status` |
| `messaging_outbox_lag_seconds` | `queue` (seconds its oldest due message has waited for a worker) |
| `messaging_relayed_total` | `outcome` (`published`, `failed`) |
| `messaging_deliveries_skipped_total` | (broker deliveries not run: duplicates, or messages already done or running) |
| `messaging_publisher_breaker_open` | `broker` (1 while publishing on commit is stopped, because the broker keeps failing) |
| `messaging_left_to_relay_total` | `reason` (`breaker`, `backlog`, `shutdown`: committed messages the publisher left to the relay) |

### Caches, logins, the audit trail, and tasks

| Metric | Labels |
| --- | --- |
| `cache_lookups_total` | `cache`, `result` (`hit`, `miss`, `invalid`, `early`) |
| `cache_errors_total` | `cache` |
| `auth_principal_cache_total` | `authentication` (`token`, `cookie`, `jwt`, `certificate`), `event` (`hit`, `miss`, `invalidation`, `eviction`, `coalesced`, `refresh`, `refresh_failed`) |
| `audit_unsealed_rows` | `database` (audit rows a sealing run left for the next one; 0 when it caught up) |
| `tasks_failed_total` | `task` (background tasks that ended with an unexpected error) |
| `tasks_restarted_total` | `task` (supervised background tasks restarted after a crash) |

### How the HTTP metrics are kept

`MetricsMiddleware` keeps the handle of each series it counts in, for at
most 4,096 series per application, so a request looks no labels up.
Clear these metrics (`REQUESTS.clear()`) only when no application is
serving. A Starlette route, or a mount inside an included router, is
labelled as it would be at the top: `unmatched`, or the mounted
application's route.

## Traces

Traces need the `tracing` extra (`domainkit[tracing]`). Commits of
[units of work](../learning.md#words) and worker messages get spans.
domainkit uses only the OpenTelemetry API: configure the SDK and the
exporter in the deployment, or run under `opentelemetry-instrument`:

```bash
opentelemetry-instrument uvicorn config.asgi:application --app-dir src
```

Outbox messages carry the W3C trace context of their commit, so a
worker's span continues the trace of the request that queued its work
([messaging](../contrib/messaging.md#traces)).

### FastAPI's own telemetry is off

FastAPI (0.142 and later) has telemetry of its own, which
`get_asgi_application` turns off: its spans record query strings, and
its log records exception messages, which domainkit keeps out of logs and
error reports. To trace requests, run under `opentelemetry-instrument`,
as before.

It is also off in the FastAPI applications mounted in domainkit's, which
keep FastAPI's defaults otherwise. The application that
`get_asgi_application` returns (`domainkit.http.application.Application`,
a `FastAPI`) sets `scope["fastapi.telemetry"]` to None on each HTTP and
WebSocket request, before any middleware or application sees it,
whatever `MIDDLEWARE` lists. FastAPI skips its telemetry for a request so
marked, as it does for one that an enclosing application measures. A mark
that an enclosing application set is kept. An application served on its
own, outside domainkit's, is the project's to configure
(`FastAPI(telemetry=...)`).

## See also

- [Operations](../operations.md): health checks, request ids, and alerts.
- [Runtime](../runtime.md#logging-and-error-reports): logging and error
  reports.
- [HTTP settings](http.md): `METRICS_TOKEN` and the access log.
