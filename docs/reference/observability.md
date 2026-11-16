# Observability

Observability is what a running service tells you about itself: its
logs, its metrics, and its traces. domainkit writes logs as JSON lines,
counts requests, messages, and caches for Prometheus, and opens spans for
OpenTelemetry. This page lists what each one holds. Metric and span names
are kept within 1.x where possible ([versioning.md](../versioning.md#what-is-public)).

On this page you find:

- what a log line holds, and what it never holds;
- every metric, with its labels;
- which work gets a span, what a request's span records, and how a trace
  crosses from a request to the worker.

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
| `db_replica_switches_total` | `database` (a read replica with `MAX_LAG`), `to` (`primary`: reads left it, past its bound or out of reach; `replica`: they came back) |
| `db_replica_lag_seconds` | `database` (how far behind its primary a replica with `MAX_LAG` was when last read) |
| `http_requests_admitted` | (requests holding a slot under `MAX_CONCURRENT_REQUESTS`) |
| `http_requests_queued` | (requests waiting for a slot) |
| `http_requests_shed_total` | `reason` (`queue_full`, `queue_timeout`) |
| `http_request_deadlines_exceeded_total` | |
| `db_statement_duration_seconds` | `database` (the alias; `<alias>:reads` for its `Reads.STATEMENTS` pool), `operation` (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `OTHER`); only with `METRICS_STATEMENTS` |

### Outgoing calls and rate limits

| Metric | Labels |
| --- | --- |
| `http_client_requests_total` | `client` (the client's name), `method` (`OTHER` for any but the nine standard ones), `outcome` (the answer's status, or `timeout`, `deadline_exceeded`, `circuit_open`, `too_large`, `unsafe_url`, `cancelled`, `error`) |
| `http_client_request_duration_seconds` | `client`, `method` (from the call to its answer, retries included) |
| `http_client_retries_total` | `client` |
| `http_client_circuits_open` | `client` (hosts whose circuit is open now, or letting a trial through; 0 while all are closed) |
| `http_client_circuit_transitions_total` | `client`, `state` (`open`, `half_open`, `closed`: the state a circuit entered) |
| `rate_limit_fallbacks_total` | `fallback` (`let_through`, `process`: decisions `RedisBackend` made while Redis was down) |

### Messaging

| Metric | Labels |
| --- | --- |
| `messaging_messages_total` | `kind`, `outcome` (`done`, `retry`, `dead`, `throttled`, `later`, `handled`, `superseded`, `unknown`, `unrecorded`, `interrupted`) |
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

### Outgoing calls

Each `HttpClient` counts its calls under its name, never a URL: give it
one with `HttpClient(name="rates")`. Without one, it is the host of its
`base_url` (`rates.example.com`), else `other`; the ready
`domainkit.http.client.client` is `default`. A name is up to 128 letters,
digits, `.`, `_`, `:`, or `-`. Clients of one name are counted together.

```python
from domainkit.http.client import HttpClient

rates = HttpClient(base_url="https://rates.example.com/v1", name="rates")
```

- A call is counted once, when it ends, with how it ended: the status of
  the answer it returned, or a word for the error it raised. Its duration
  runs from the call to its answer, its retries and their waits included.
  Each retry is counted too.
- A client keeps a circuit per host (see
  [runtime.md](../runtime.md#outgoing-http-calls)).
  `http_client_circuits_open` counts, at each scrape, its hosts whose
  circuit is not closed, so it holds no host name. Each client with a
  breaker has a series from the start, at 0.
  `http_client_circuit_transitions_total` counts each circuit that opened
  (after `BREAKER_FAILURES` failures in a row, or after a failed trial),
  let a trial through after `BREAKER_COOLDOWN` (`half_open`), or closed
  (its host answered, or it was forgotten for newer ones past 1,024
  hosts).

### Rate limits and statements

- `rate_limit_fallbacks_total` counts each request `RedisBackend` decided
  while Redis was down or skipped after a failure: `let_through`, or
  counted in the `process` at its share of the limit
  (`RATE_LIMIT_FALLBACK_REPLICAS`). It grows only during an outage.
- `METRICS_STATEMENTS = True` times every statement the pools' connections
  send, from the moment SQLAlchemy hands it to the driver to its answer
  or its failure. The label is its first keyword, never its text or its
  values. It is off by default; on, each statement costs 13 Python calls
  more ([performance.md](../performance.md#what-observability-costs)).

## Traces

Traces need the `tracing` extra (`domainkit[tracing]`). Requests, with
`TracingMiddleware`, commits of [units of work](../learning.md#words),
and worker messages get spans. domainkit uses only the OpenTelemetry API:
configure the SDK and the exporter in the deployment, or run under
`opentelemetry-instrument`:

```bash
opentelemetry-instrument uvicorn config.asgi:application --app-dir src
```

Outbox messages carry the W3C trace context of their commit, so a
worker's span continues the trace of the request that queued its work
([messaging](../contrib/messaging.md#traces)).

### Requests

List `domainkit.http.tracing.TracingMiddleware` right after
`ProxyHeadersMiddleware` ([the middleware order](http.md#middleware)).
Each HTTP request then runs in a span of kind `SERVER`. Its commits, the
messages it queues, its outgoing calls, and its log lines (the access
line among them) belong to that span's trace:

```python
MIDDLEWARE = [
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.tracing.TracingMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.deadlines.DeadlineMiddleware",
    "domainkit.http.shedding.ConcurrencyLimitMiddleware",
    "domainkit.http.middleware.AllowedHostsMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
```

The span follows OpenTelemetry's conventions for HTTP server spans:

| Name or attribute | Value |
| --- | --- |
| name | the method and the route's template (`GET /accounts/{account_id}`); the method alone when no route matched; `HTTP` for a method it does not know |
| `http.request.method` | the method, or `_OTHER` (`http.request.method_original` then holds it) |
| `http.route` | the route's template, when one matched |
| `http.response.status_code` | the status of the answer |
| `url.scheme`, `network.protocol.version` | as the request came, behind trusted proxies as they said |
| `url.path` | the path as sent, only with `ACCESS_LOG_PATH = "raw"` |
| `error.type` | the status from 500 up, or the type of an error that escaped; the span's status is then an error |

- The query string, headers, bodies, and error messages are never
  recorded, as in the logs. Nor is the path as sent unless
  `ACCESS_LOG_PATH` is `"raw"`: paths carry ids, tokens, and emails.
- A request continues the trace of its `traceparent` (and `tracestate`)
  as `ACCEPT_TRACE_CONTEXT` says, like `ACCEPT_REQUEST_ID`: a trusted
  proxy's only (`"proxies"`, the default), any caller's (`"anyone"`, for
  a service that other services of yours call), or nobody's (`"never"`).
  A caller's own trace context would name its requests in the logs, and
  could ask a sampler to keep every one of its requests.
- A request already inside a span, which an enclosing instrumentation
  opened, gets none of its own.
- WebSocket connections get no span.
- Without an SDK configured, spans are dropped, at a small cost per
  request.

### FastAPI's own telemetry is off

FastAPI (0.142 and later) has telemetry of its own, which
`get_asgi_application` turns off: its spans record query strings, and
its log records exception messages, which domainkit keeps out of logs and
error reports. To trace requests, list `TracingMiddleware`
([Requests](#requests)), or run under `opentelemetry-instrument`.

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
