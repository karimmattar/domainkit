# 0099. Take request ids from trusted proxies only, and drop a caller's own

- Status: Accepted
- Date: 2026-10-26

## Context

A request's id names it in its log lines, its error reports, its audit
rows, the work it queues (the outbox carries it, and the worker's lines
and rows name it), its outgoing calls (`X-Request-ID`), and its answer.
Up to 0.10, `RequestContextMiddleware` took the id from the caller's
`X-Request-ID` whenever it was up to 128 letters, digits, `.`, `_`, `:`,
or `-` (audit 0.9.0, WEB-13). The format check, there since 0.9, kept line
breaks and quotes out of log lines; but any client chose the id, so it
could:

- give its requests the id of another request, seen in an error body or
  a support ticket, and have its lines read as that request's;
- give one id to many requests, from many addresses, so a search by id
  returns lines that do not belong together;
- send a chosen id on to the services the application calls, which log
  and propagate it in turn.

The proxy boundary is already decided once per request:
`ProxyHeadersMiddleware` believes a connection's headers only when its
peer is in `TRUSTED_PROXIES`, a unix socket's peer included with
`"unix"` (ADR 0072), and keeps that peer in `scope["domainkit.peer"]`.

The options for a caller's value:

- **Drop it**: a new id, which the answer's `X-Request-ID` gives back,
  so the caller can still quote it.
- **Keep it apart**: a second, sanitized field (`client_request_id`) in
  log lines. It needs the header read on every request, a place on the
  request context (a public class) for the field, and the log filter and
  formatters to carry it.
- **Trust it from the proxy's networks only, checked again** in the
  request middleware: the address parsed a second time.

Measured with the benchmark ladder (Python calls per request, against
0.10.0's tree), on the rungs without a database, where a client sends no
`X-Request-ID`:

| Rung | 0.10.0 | Drop | Keep apart (header read and kept in the scope only) |
| --- | --- | --- | --- |
| `default` | 415 | 414 | 415 |
| `default-json` | 560 | 559 | 560.3 |
| `template` | 530 | 529 | 530 |

Dropping costs nothing: without a trusted peer the header is not even
read, one call fewer than 0.10. Keeping a caller's value apart costs what
0.10 cost before any log line carries it, and more once one does.

## Decision

- The request id is the `X-Request-ID` of a request whose connection
  `ProxyHeadersMiddleware` trusted (`domainkit.peer` in the scope: the
  same `TRUSTED_PROXIES` rule as the client's address, unix sockets
  included), when it has the format above; else a new id
  (`new_request_id`, 32 hex digits). The check is one lookup in the
  scope.
- Of several `X-Request-ID` lines, the last is read: the nearest
  proxy's, as the scheme is (ADR 0072). A proxy that sets the header by
  adding a line (HAProxy's `unique-id-header`, Apache's `RequestHeader
  add`, Caddy's `header_up +X-Request-ID`, Envoy's append mode) leaves a
  client's line in front of its own, and ASGI servers pass both on in
  order; up to the security review of 0.11 the first line was read, so
  behind such a proxy a caller still chose its id (SEC-11c-1). Minting a
  new id whenever there are several lines was the other option: it is as
  safe, but lets any caller cut its requests off from the proxy's logs by
  sending a line of its own, where the last line keeps the proxy's id.
  Reading the last line costs no call more than reading the first.
- A caller's own value is dropped: not used, not logged, not kept.
- `ACCEPT_REQUEST_ID` (`"proxies"` by default) can take any caller's
  (`"anyone"`, 0.10's rule, for a service whose callers are its own) or
  nobody's (`"never"`, for a proxy that does not set the header). Both
  keep the format check.
- The proxy must set the header, by overwriting it or adding its line
  last: most forward a client's headers unless told otherwise, as with
  the scheme (ADR 0072). The operations page shows nginx's
  `proxy_set_header X-Request-ID $request_id;` and HAProxy's
  `unique-id-header X-Request-ID`.

## Consequences

- A client cannot choose, share, or borrow request ids; behind a proxy
  that sets them, ids correlate the proxy's logs with the application's.
- A client that sent its own id to correlate its calls gets the
  service's id back in the answer instead; projects whose callers relied
  on theirs set `ACCEPT_REQUEST_ID = "anyone"`.
- A deployment whose proxy passes headers through and is trusted still
  takes the client's id until the proxy sets it or `"never"` is set:
  the reference and the hardening checklist say so. A proxy that adds a
  line before a client's, rather than after, is not one we know of; it
  would need `"never"` too.
- `RequestContextMiddleware` must come after `ProxyHeadersMiddleware`, as
  in the default order and the template; placed before it, no id is
  taken, which fails safe.
