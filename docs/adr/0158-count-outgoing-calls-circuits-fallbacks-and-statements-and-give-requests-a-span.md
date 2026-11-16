# 0158. Count outgoing calls, circuits, fallbacks, and statements, and give requests a span

- Status: Accepted
- Date: 2026-11-16
- Amends: [0065](0065-upgrade-fastapi-sqlalchemy-asyncpg-behind-the-ladder.md)

## Context

A project that runs an identity service on domainkit listed what it
could not see (user report W9): no metrics for outgoing HTTP calls and
their circuits, for the rate limiter's fallback, or for statement
latency; and no span per request, since FastAPI's telemetry is forced
off and domainkit opens none of its own. The audit confirmed it:
`domainkit.http.metrics` counts requests, pools, and admission only;
`domainkit.http.client` counts nothing; `domainkit.tracing` spans commits
and worker messages; and [ADR 0065](0065-upgrade-fastapi-sqlalchemy-asyncpg-behind-the-ladder.md)
left request spans to `opentelemetry-instrument`. So a commit's span has
no parent, and a slow request cannot be broken down.

The conventions that apply:

- OpenTelemetry's [HTTP metrics](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/)
  time each client and server request (`http.client.request.duration`,
  `http.server.request.duration`, in seconds), by method, status, and
  `error.type`, a low-cardinality word for a failure. The client's
  `server.address` is required, `url.template` opt-in.
- Its [HTTP spans](https://opentelemetry.io/docs/specs/semconv/http/http-spans/)
  name a server span `{method} {http.route}`, never after the path, of
  kind `SERVER`. A 5xx sets its status to error, a 4xx leaves it unset.
  `url.query` is to be recorded when present, and sensitive content
  scrubbed where the instrumentation can tell.
- Its [database metrics](https://opentelemetry.io/docs/specs/semconv/database/database-metrics/)
  time each operation (`db.client.operation.duration`) by system,
  namespace, and operation name; the query's text is opt-in only.
- Prometheus's [naming](https://prometheus.io/docs/practices/naming/)
  and [instrumentation](https://prometheus.io/docs/practices/instrumentation/)
  pages ask for base units in the name (`_seconds`), `_total` on
  counters, few labels, never user ids or other unbounded values, and
  series that exist before their first event. domainkit's own names
  already read so (`http_request_duration_seconds`,
  `messaging_messages_total`, `db_pool_connections`).
- Libraries of other stacks label a breaker by its name, with a gauge of
  its state and a count of calls it refused
  ([Resilience4j](https://resilience4j.readme.io/docs/micrometer)), and
  time statements by database alias
  ([django-prometheus](https://github.com/korfuri/django-prometheus),
  through a database backend a project opts into).
- The W3C [Trace Context](https://www.w3.org/TR/trace-context/)'s
  security considerations warn that continuing any caller's sampled
  trace lets an attacker overwhelm the tracing, and suggest restarting
  the trace at a trust boundary. [ADR 0099](0099-take-request-ids-from-trusted-proxies-only.md)
  already takes request ids from trusted proxies only.

The options weighed for request spans:

- **Turn FastAPI's telemetry back on.** Its server span records the
  path as sent and the query string, with five parameter names
  redacted, and its logs exception messages; no option leaves them out.
  ADR 0065's reasons stand.
- **Keep leaving it to `opentelemetry-instrument`.** It wraps the
  process, records URLs as its instrumentations do, and a project without
  it has commit spans without a parent.
- **Open a span in the application for every request once an SDK is
  set**, as FastAPI does. Every deployment with an SDK would export new
  spans on upgrade, and the application, outside `MIDDLEWARE`, cannot
  know whether a `traceparent` came from a trusted proxy.
- **A middleware the project lists**, as it lists `MetricsMiddleware`.
  It costs nothing unlisted, and sits after `ProxyHeadersMiddleware`,
  which tells it whom to believe.

For the metrics: OpenTelemetry's metrics API would add a second way to
export; domainkit's metrics are Prometheus's, at `/metrics` and
`runworker --metrics-port`, so the new ones are too, named in its style
for what the OpenTelemetry metrics measure. A client's `server.address`
as a label would be unbounded for a client whose URLs someone else
chooses (a guarded client, or the shared `client`). A per-host circuit
gauge would be too. Timing every statement costs every database request,
which the ladder gates (ADR 0064).

## Decision

- **Outgoing calls.** `HttpClient(name=...)` names a client in its
  metrics: by default the host of its `base_url`, else `other`; the
  shared `client` is `default`. A name is up to 128 letters, digits, `.`,
  `_`, `:`, or `-`. Each call is counted once it ends, in
  `http_client_requests_total{client, method, outcome}`, and timed from
  the call to its answer, retries and their waits included, in
  `http_client_request_duration_seconds{client, method}` (the request
  metrics' buckets). `outcome` is the answer's status, or `timeout`,
  `deadline_exceeded`, `circuit_open`, `too_large`, `unsafe_url`,
  `cancelled`, or `error`. `method` is one of the nine standard methods,
  else `OTHER`. `http_client_retries_total{client}` counts retries.
- **Circuits.** `http_client_circuit_transitions_total{client, state}`
  counts each circuit that opens (from closed, or after a failed trial),
  lets a trial through (`half_open`), or closes (its host answered, or it
  was forgotten past 1,024 hosts). `http_client_circuits_open{client}` is
  counted from the clients at each scrape: their hosts whose circuit is
  not closed, 0 for each client with a breaker. No host is a label.
- **Rate limits.** `rate_limit_fallbacks_total{fallback}` counts each
  decision `RedisBackend` makes while Redis is down or skipped:
  `let_through`, or counted in the `process` by its fallback.
- **Statements.** `METRICS_STATEMENTS = True` times each statement of the
  pools in `db_statement_duration_seconds{database, operation}`, from
  SQLAlchemy's hand-off to the driver to the answer or the failure.
  `database` is the alias (`<alias>:reads` for its `Reads.STATEMENTS`
  pool, as `pool_stats` names it); `operation` the first keyword:
  `SELECT`, `INSERT`, `UPDATE`, `DELETE`, else `OTHER`; never the text.
  It is off by default.
- **Request spans.** `domainkit.http.tracing.TracingMiddleware`, listed
  right after `ProxyHeadersMiddleware`, runs each HTTP request in a span
  of kind `SERVER`, named `{method} {route template}` (the method alone
  when no route matched; `HTTP` for an unknown method, recorded as
  `_OTHER`). It records `http.request.method`, `url.scheme`,
  `network.protocol.version`, `http.route`, `http.response.status_code`,
  and `error.type` (the status from 500 up, or the type of an error that
  escaped, which sets the status to error, never a message). Never the
  query string, a header, or a body; `url.path` only with
  `ACCESS_LOG_PATH = "raw"`, as the logs show paths. The request's
  commits, the messages they queue, its outgoing calls, and its log lines
  belong to its trace.
- **Whose trace.** `ACCEPT_TRACE_CONTEXT` says whose `traceparent` a
  request continues, as `ACCEPT_REQUEST_ID` does for ids: a trusted
  proxy's (`"proxies"`, the default), any caller's (`"anyone"`), or
  nobody's (`"never"`). One `traceparent` line is read, and the
  `tracestate` lines joined.
- A request already inside a recording span gets none of its own; until
  an SDK is set, none is opened, and a continued trace context only
  passes on. WebSocket connections get no span. FastAPI's telemetry stays
  off.

## Consequences

- The names and labels above are public, under `docs/versioning.md`'s
  rule for metrics and spans; changing one needs a changelog entry of
  its own.
- What it costs, on the ladder: nothing for a project that lists no
  `TracingMiddleware` and leaves `METRICS_STATEMENTS` off (the calls and
  modules of every rung measured are unchanged). Listed without an SDK,
  the middleware costs 9 to 12 calls a request; with an SDK and no
  processor, about 260, the SDK's own span. `METRICS_STATEMENTS` costs
  13 calls a statement and one module (docs/performance.md).
- An outgoing call costs a few label lookups, small beside the network.
  A scrape walks each client's circuits under its lock.
- A service that only your services call sets
  `ACCEPT_TRACE_CONTEXT = "anyone"` to join their traces; one behind a
  load balancer that starts traces lists the balancer in
  `TRUSTED_PROXIES`.
- The client opens no span of its own per attempt, as the HTTP span
  conventions suggest: its calls carry the trace context of the request's
  span, and their time is in the metrics. A client span can be added
  later without changing these names.
- ADR 0065's "deployments that want request spans run under
  `opentelemetry-instrument`" is amended: they list `TracingMiddleware`,
  or keep running under it.
