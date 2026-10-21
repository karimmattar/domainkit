# 0050. Call other services through pooled clients that keep to the deadline

- Status: Accepted
- Date: 2026-10-20

## Context

Services call other services: payment providers, rate tables, identity
providers, URLs their users give them. Each project wrote its own calls:
a client per call (a handshake each time), no bound tied to the request
that waits, POSTs retried after a timeout (charged twice), retries at
the same moment from every replica, a failing host called again and again,
URLs with tokens in logs, and user-supplied URLs reaching internal
addresses. `domainkit.contrib.webhooks` already had a careful sender and an
SSRF guard, for its one use.

The choices were the library, how clients are made and closed, what is
retried, how a failing host is handled, and how the guard holds with
pooled connections.

- **Library.** httpx2 already serves the test client and webhooks; another
  library would add a dependency and a second way to stub.
- **Lifetime.** A client per call pays a handshake each time and leaves
  sockets to the garbage collector; one shared client cannot hold
  different options per service. Async connections belong to one event
  loop, and tests and commands run several.
- **Retries.** Retrying everything sends non-idempotent calls twice;
  retrying nothing turns every dropped connection into an error. Fixed
  waits synchronise retries across clients; full jitter spreads them.
- **The guard with a pool.** Webhooks rewrite the URL to the checked
  address and keep the name for SNI. A pool keyed by address would then
  carry calls for one name over a connection whose certificate was
  checked for another. Checking inside the step that opens a connection
  keeps the pool keyed by name and the certificate checked against it.

## Decision

- `domainkit.http.client.HttpClient`, in a new `httpclient` extra
  (httpx2), is made once per service called. It opens one pool per event
  loop at its first call; `close_resources` closes them, so the
  application's lifespan and every command do.
- Every call is bounded by its `timeout`, retries included, and by the
  deadline (ADR 0048); httpx2's own timeouts get the time left.
- Retries (`RETRIES`, at most 10) follow network errors, timeouts, and 429,
  502, 503, and 504, for idempotent methods or `retry=True`; a call that
  could not connect is retried whatever its method; `retry=False` never.
  Waits are full jitter below `BACKOFF` doubled per retry, capped by
  `MAX_BACKOFF`, which also caps `Retry-After`; a retry that could not
  finish in time is not started.
- Each client keeps a circuit per host: `BREAKER_FAILURES` failures in a
  row (network errors, timeouts, 5xx) open it for `BREAKER_COOLDOWN`
  seconds, during which calls raise `CircuitOpen` at once; then one call
  at a time tries the host. Calls cut off by the deadline, or cancelled,
  count neither way. At most 1024 failing hosts are remembered.
- Calls send `X-Request-ID` and the W3C trace context, read at most
  `MAX_RESPONSE_BYTES` after decompression, follow no redirects, and are
  logged by method, origin, and path only. A relative URL is appended to
  `base_url`, and a URL naming another origin is refused.
- The SSRF guard moves from webhooks to `domainkit.http.guard`, with
  webhooks' names, messages, codes, and behaviour kept. `guard=True`
  checks the URL before sending and every resolved address as each
  connection opens (an httpcore2 network backend), connects to a checked
  address, ignores proxies from the environment, asks for uncompressed
  answers, and sends no request id or trace context unless asked.
- `stub(handler)` routes every call in a block through httpx2's
  `MockTransport`; `transport=` does it for one client.

## Consequences

- Calls reuse connections, end when the work they serve must end, and stop
  hammering a host that is down, without per-project code.
- A request that waits on a slow service gets 503 `deadline_exceeded`
  rather than hanging; one whose calls fail fast with `CircuitOpen` should
  answer 503 or degrade, as the project chooses.
- Non-idempotent calls are retried only when nothing was sent, or when
  the caller says so; an idempotency key the other service honours makes
  `retry=True` safe.
- Unguarded clients honour proxy variables and `SSL_CERT_FILE` from the
  environment, as httpx2 does; guarded clients ignore them, so a
  deployment that needs an egress proxy for user URLs needs a change here.
- Paths are logged: secrets belong in headers, or behind a guarded client,
  whose logs show only the origin.
- A client made per call, against the advice, opens a pool per call,
  dropped unclosed when the client is collected.

## Addendum (2026-10-21): what a security review changed

- **Circuits and trust domains.** A client keeps its circuits per host for
  every caller: one user's failing URLs on a host opened the circuit for
  every user of a shared guarded client. `HttpClient(breaker=False)` keeps
  none. Make one client per trust domain, and `breaker=False` for one that
  calls URLs its users chose.
- **Lookups on threads of the guard's own.** The guard resolved host
  names on the loop's default executor, and a timeout only stopped the
  waiting: names whose DNS never answers held its threads, and with them
  `asyncio.to_thread` and new database connections. The guard now looks
  names up on a few threads of its own (4), at most 2 of one host at
  once, remembers a failure for 5 seconds, and fails a lookup past those
  bounds at once with an `OSError` (`EAI_AGAIN`), which a call takes as a
  connect error and webhooks as a failed attempt, never as a refused URL.
  The threads stop at shutdown, in the resources phase.
- **TLS trust.** `trust_env=False` was meant to make guarded clients
  ignore `SSL_CERT_FILE`, but on Linux the system store is OpenSSL's
  default paths, which follow it. Guarded clients and webhooks now load
  the CAs where OpenSSL was built to look (or the system's bundle), never
  where the environment says, unless `CA_BUNDLE` names a bundle; on macOS
  and Windows the operating system's store is used.
- **`base_url`.** A relative URL may not hold a `.` or `..` segment,
  spelled out or percent-encoded, so input in a path cannot climb out of
  the base path. A guarded client refuses `transport=`, which would skip
  its address checks; tests use `stub`.
- **Logs.** httpx2 logs each call's full URL, query string included, at
  INFO: `build_logging_config` sets its loggers, and httpcore2's, to
  WARNING.
