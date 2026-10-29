# Observability

**Logs** are JSON lines with `request_id`, and with `trace_id` and `span_id`
when a span is active. Query strings and bound SQL values are never logged.
They are written by a thread, never by the event loop; a burst past the
queue's 10,000 records is dropped and counted, never waited for
([runtime.md](../runtime.md#logging-and-error-reports)).

**Metrics** (`domainkit[metrics]`, mounted at `/metrics`):

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
| `messaging_messages_total` | `kind`, `outcome` |
| `messaging_message_duration_seconds` | `kind` |
| `messaging_outbox_messages` | `status` |
| `messaging_outbox_lag_seconds` | `queue` (seconds its oldest due message has waited for a worker) |
| `messaging_relayed_total` | `outcome` (`published`, `failed`) |
| `messaging_deliveries_skipped_total` | |
| `cache_lookups_total` | `cache`, `result` (`hit`, `miss`, `invalid`, `early`) |
| `cache_errors_total` | `cache` |
| `auth_principal_cache_total` | `authentication` (`token`, `cookie`, `jwt`, `certificate`), `event` (`hit`, `miss`, `invalidation`, `eviction`, `coalesced`, `refresh`, `refresh_failed`) |

`MetricsMiddleware` keeps the handle of each series it counts in, for at
most 4,096 series per application, so a request looks no labels up; clear
these metrics (`REQUESTS.clear()`) only when no application is serving. A
Starlette route or a mount inside an included router is labelled as it
would be at the top: `unmatched`, or the mounted application's route.

**Traces** (`domainkit[tracing]`): unit-of-work commits and worker messages
get spans. domainkit only uses the OpenTelemetry API; configure the SDK and
exporter in the deployment, or run under `opentelemetry-instrument`. Outbox
messages carry the W3C trace context of their commit, so a worker's span
continues the trace of the request that queued its work
([contrib.md](../contrib/messaging.md#traces)). FastAPI (0.142 and later) has telemetry
of its own, which `get_asgi_application` turns off: its spans record query
strings, and its log records exception messages, which domainkit keeps out
of logs and error reports. To trace requests, run under
`opentelemetry-instrument`, as before.

It is off in the FastAPI applications mounted in domainkit's too, which
keep FastAPI's defaults otherwise: the application `get_asgi_application`
returns (`domainkit.http.application.Application`, a `FastAPI`) sets
`scope["fastapi.telemetry"]` to None on each HTTP and WebSocket request
before any middleware or application sees it, whatever `MIDDLEWARE` lists,
and FastAPI skips its telemetry for a request so marked, as it does for
one an enclosing application measures. A mark an enclosing application
set is kept. An application served on its own, outside domainkit's, is
the project's to configure (`FastAPI(telemetry=...)`).
